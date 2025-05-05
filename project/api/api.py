import os
import uuid
import logging
import aiofiles
import traceback
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, Body
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import aiomysql
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, List
import asyncio
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.DEBUG,
    filename='api.log',
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class OrderUpdate(BaseModel):
    status: Optional[str] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.db_pool = await aiomysql.create_pool(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "3465"),
        db=os.getenv("DB_NAME", "send_to_print"),
        auth_plugin='mysql_native_password',
        minsize=5,
        maxsize=20
    )
    yield
    app.db_pool.close()
    await app.db_pool.wait_closed()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


@app.get("/orders", response_model=List[dict])
async def get_orders(
        status: List[str] = Query(..., title="Статусы заказов"),
        shop_id: Optional[int] = Query(None, title="ID магазина")
):
    allowed_statuses = {"получен", "готов"}  # "выдан" исключён
    try:
        allowed_statuses = {"получен", "готов"}
        invalid_statuses = [s for s in status if s not in allowed_statuses]
        if invalid_statuses:
            raise HTTPException(400, detail=f"Недопустимые статусы: {invalid_statuses}")

        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Формируем IN-условие для статусов
                placeholders = ",".join(["%s"] * len(status))
                query = f"SELECT * FROM `order` WHERE status IN ({placeholders})"
                params = status.copy()

                # Добавляем фильтр по магазину
                if shop_id is not None:
                    query += " AND ID_shop = %s"
                    params.append(shop_id)

                # Выполняем запрос
                await cursor.execute(query, params)
                orders = await cursor.fetchall()

                # Приводим типы данных
                for order in orders:
                    order["ID"] = int(order["ID"])
                    order["price"] = float(order["price"])
                    order["pages"] = int(order["pages"])

                return orders or []

    except aiomysql.Error as e:
        logging.error(f"Ошибка БД: {str(e)}")
        raise HTTPException(500, detail="Ошибка базы данных")
    except Exception as e:
        logging.error(f"Ошибка: {traceback.format_exc()}")
        raise HTTPException(500, detail="Ошибка сервера")


@app.post("/orders/{order_id}")
async def update_order_status(order_id: int, data: OrderUpdate):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE `order` SET status = %s WHERE ID = %s",
                    (data.status, order_id)
                )
                await conn.commit()

                if cursor.rowcount == 0:
                    raise HTTPException(404, detail="Заказ не найден")

                return {"status": "готов"}

    except aiomysql.Error as e:
        await conn.rollback()
        logging.error(f"Ошибка БД: {str(e)}")
        raise HTTPException(500, detail="Ошибка базы данных")


@app.post("/orders/{order_id}/complete")
async def complete_order(order_id: int, data: OrderUpdate):  # Используем OrderUpdate
    try:
        allowed_statuses = {"готов", "выдан"}  # Разрешаем только эти статусы
        if data.status not in allowed_statuses:
            raise HTTPException(400, detail="Недопустимый статус")

        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE `order` SET status = %s WHERE ID = %s",
                    (data.status, order_id)
                )
                await conn.commit()
                return {"status": data.status}  # Возвращаем актуальный статус
    except Exception as e:
        await conn.rollback()
        raise HTTPException(500, detail="Ошибка сервера")


@app.post("/orders")
async def create_order(
        file: UploadFile = File(...),
        ID_shop: int = Form(...),
        price: float = Form(...),
        pages: int = Form(...),
        color: str = Form(...),
        user_id: str = Form(...),
        note: str = Form(''),
        con_code: int = Form(...),
        file_extension: str = Form(...)
):
    temp_filename = f"temp_{uuid.uuid4()}{os.path.splitext(file.filename)[1]}"
    temp_path = os.path.join(UPLOAD_FOLDER, temp_filename)

    try:
        # Сохранение временного файла
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(await file.read())

        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()

                try:
                    await cursor.execute(
                        """INSERT INTO `order` 
                        (ID_shop, price, note, con_code, color, status, 
                         user_id, pages, file_extension, file_path) 
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (ID_shop, price, note, con_code, color, 'получен',
                         user_id, pages, file_extension, temp_filename)
                    )
                    order_id = cursor.lastrowid

                    # Переименование файла
                    new_filename = f"order_{order_id}{os.path.splitext(temp_filename)[1]}"
                    new_path = os.path.join(UPLOAD_FOLDER, new_filename)

                    if os.path.exists(temp_path):
                        await asyncio.to_thread(os.rename, temp_path, new_path)

                    await cursor.execute(
                        "UPDATE `order` SET file_path = %s WHERE ID = %s",
                        (new_filename, order_id)
                    )
                    await conn.commit()

                    return JSONResponse(
                        content={"order_id": order_id, "con_code": con_code},
                        status_code=201
                    )

                except Exception as e:
                    await conn.rollback()
                    if os.path.exists(temp_path):
                        await asyncio.to_thread(os.remove, temp_path)
                    raise

    except Exception as e:
        logging.error(f"Ошибка создания заказа: {traceback.format_exc()}")
        raise HTTPException(500, detail=str(e))


@app.get("/shops")
async def get_shops():
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address FROM shop"
                )
                shops = await cursor.fetchall()
                return shops or JSONResponse(
                    content={"message": "Магазины не найдены"},
                    status_code=404
                )

    except Exception as e:
        logging.error(f"Ошибка: {traceback.format_exc()}")
        raise HTTPException(500, detail="Ошибка сервера")


@app.get("/shops/{shop_name}")
async def get_shop(shop_name: str):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address, price_bw, price_cl FROM shop WHERE name = %s",
                    (shop_name,)
                )
                shop = await cursor.fetchone()
                if not shop:
                    raise HTTPException(status_code=404, detail="Магазин не найден")
                return shop
    except Exception as e:
        logging.error(f"Ошибка: {traceback.format_exc()}")
        raise HTTPException(500, detail="Ошибка сервера")


@app.get("/shop/{password_hash}")
async def get_shop_by_password(password_hash: str):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT ID_shop, name FROM shop WHERE password = %s",
                    (password_hash,)
                )
                shop = await cursor.fetchone()
                return shop or {"detail": "Магазин не найден"}
    except Exception as e:
        logging.error(f"Ошибка: {traceback.format_exc()}")
        raise HTTPException(500, detail="Ошибка сервера")


@app.get("/files/{filename}")
async def get_file(filename: str):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        raise HTTPException(404, detail="Файл не найден")
    return FileResponse(file_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)