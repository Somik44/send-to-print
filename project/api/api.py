import os
import uuid
import logging
import aiofiles
import traceback
import json
import asyncio
import websockets
import aiomysql
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, WebSocket
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
from decimal import Decimal
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect
from json import JSONDecodeError
from starlette.websockets import WebSocketState, WebSocketDisconnect

logging.basicConfig(
    level=logging.DEBUG,
    filename='api.log',
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class OrderUpdate(BaseModel):
    status: Optional[str] = None


app = FastAPI()
WS_URL = 'ws://localhost:5000/bot'
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


# Database configuration
async def get_db():
    return await aiomysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "Qwerty123"),
        db=os.getenv("DB_NAME", "send_to_print"),
        autocommit=False,
        cursorclass=aiomysql.DictCursor
    )


@app.websocket("/ws/notify")
async def websocket_notify(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        logging.error(f"WebSocket connection closed: {str(e)}")


async def notify_bot(order_id: int, status: str):
    try:
        # Используем существующее подключение через get_db()
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT o.user_id, o.ID, s.address 
                    FROM `order` o 
                    JOIN shop s ON o.ID_shop = s.ID_shop 
                    WHERE o.ID = %s
                """, (order_id,))
                data = await cursor.fetchone()

        # Исправляем адрес WebSocket на порт 8001
        async with websockets.connect("ws://localhost:8001") as ws:
            await ws.send(json.dumps({
                "type": "status_update",
                "status": status,
                "user_id": data['user_id'],
                "order_id": data['ID'],
                "address": data['address']
            }))
    except Exception as e:
        logging.error(f"WebSocket notification error: {traceback.format_exc()}")


# Helper functions
def decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# Orders endpoints
@app.get("/orders", response_model=List[dict])
async def get_orders(
        status: List[str] = Query(..., title="Статусы заказов"),
        shop_id: Optional[int] = Query(None, title="ID магазина")
):
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(status))
                query = f"SELECT * FROM `order` WHERE status IN ({placeholders})"
                params = status.copy()

                if shop_id is not None:
                    query += " AND ID_shop = %s"
                    params.append(shop_id)

                await cursor.execute(query, params)
                result = await cursor.fetchall()
                await conn.commit()
                return result

    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.post("/orders/{order_id}/ready")
async def mark_order_ready(order_id: int):
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()
                # Получаем user_id перед изменением статуса
                await cursor.execute(
                    "SELECT user_id FROM `order` WHERE ID = %s FOR UPDATE",
                    (order_id,)
                )
                current = await cursor.fetchone()

                if not current:
                    await conn.rollback()
                    raise HTTPException(404, detail="Order not found")

                # Обновляем статус
                await cursor.execute(
                    "UPDATE `order` SET status = 'готов' WHERE ID = %s",
                    (order_id,)
                )
                await conn.commit()
                await notify_bot(order_id, "готов")
                return {"status": "готов"}
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


@app.post("/orders/{order_id}/complete")
async def complete_order(order_id: int):
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()

                # 1. Получаем данные заказа с блокировкой
                await cursor.execute(
                    """SELECT status, user_id, file_path 
                       FROM `order` 
                       WHERE ID = %s 
                       FOR UPDATE""",
                    (order_id,)
                )
                current = await cursor.fetchone()

                if not current:
                    await conn.rollback()
                    raise HTTPException(404, detail="Order not found")

                if current['status'] != 'готов':
                    await conn.rollback()
                    raise HTTPException(
                        400,
                        detail=f"Невозможно завершить заказ в статусе {current['status']}"
                    )

                # 2. Удаление файла
                file_path = os.path.join(UPLOAD_FOLDER, current['file_path'])
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logging.info(f"Файл заказа {order_id} удален: {file_path}")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла: {str(e)}")
                    # Не прерываем выполнение, только логируем

                # 3. Обновление статуса
                await cursor.execute(
                    "UPDATE `order` SET status = 'выдан' WHERE ID = %s",
                    (order_id,)
                )
                await conn.commit()
                await notify_bot(order_id, "выдан")
                return {"status": "выдан"}

    except HTTPException:
        raise
    except Exception as e:
        await conn.rollback()
        logging.error(f"Ошибка завершения заказа: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


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
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                # Create order record
                await cursor.execute("""
                    INSERT INTO `order` (
                        ID_shop, price, note, con_code, color, status, 
                        user_id, pages, file_extension, file_path
                    ) VALUES (%s, %s, %s, %s, %s, 'получен', %s, %s, %s, 'temp')
                """, (
                    ID_shop, price, note, con_code, color,
                    user_id, pages, file_extension
                ))
                order_id = cursor.lastrowid

                # Generate filename
                new_filename = f"order_{order_id}{os.path.splitext(file.filename)[1]}"
                new_path = os.path.join(UPLOAD_FOLDER, new_filename)

                # Save file
                async with aiofiles.open(new_path, 'wb') as f:
                    await f.write(await file.read())

                # Update file path
                await cursor.execute(
                    "UPDATE `order` SET file_path = %s WHERE ID = %s",
                    (new_filename, order_id))

                await conn.commit()
                return JSONResponse(
                    content={"order_id": order_id, "con_code": con_code},
                    status_code=201
                )

    except Exception as e:
        if 'new_path' in locals() and os.path.exists(new_path):
            os.remove(new_path)
        logging.error(f"Order creation error: {traceback.format_exc()}")
        raise HTTPException(500, detail=str(e))


# Shops endpoints
@app.get("/shops")
async def get_shops():
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT name, ID_shop, address FROM shop")
                shops = await cursor.fetchall()
                return shops or JSONResponse(
                    content={"message": "No shops found"},
                    status_code=404
                )
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.get("/shops/{shop_name}")
async def get_shop(shop_name: str):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address, price_bw, price_cl FROM shop WHERE name = %s",
                    (shop_name,)
                )
                shop = await cursor.fetchone()
                if not shop:
                    raise HTTPException(status_code=404, detail="Shop not found")
                return shop
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.get("/shop/{password_hash}")
async def get_shop_by_password(password_hash: str):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:  # Используем DictCursor
                await cursor.execute(
                    "SELECT ID_shop, name, address FROM shop WHERE password = %s",  # Добавили address
                    (password_hash,)
                )
                shop = await cursor.fetchone()
                if not shop:
                    return JSONResponse(
                        content={"detail": "Invalid password"},
                        status_code=401
                    )
                return shop  # Теперь возвращает ID, название и адрес
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


# Files endpoint
@app.get("/files/{filename}")
async def get_file(filename: str):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        raise HTTPException(404, detail="File not found")
    return FileResponse(file_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)