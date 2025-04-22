import os
import uuid
import logging
import aiofiles
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
        host='localhost',
        user='root',
        password='Qwerty123',
        db='send_to_print',
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
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


@app.get("/orders")
async def get_orders(status: List[str] = Query(..., alias="status[]")):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                placeholders = ','.join(['%s'] * len(status))
                await cursor.execute(
                    f"SELECT * FROM `order` WHERE status IN ({placeholders})",
                    status
                )
                orders = await cursor.fetchall()
                for order in orders:
                    order['price'] = float(order['price'])
                return orders
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/order")
async def update_order_status(
        id: int = Query(..., title="ID"),  # Явное указание типа
        data: dict = Body(...)
):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # Исправленный SQL-запрос
                await cursor.execute(
                    "UPDATE `order` SET status = %s WHERE ID = %s",
                    (data['status'], id)
                )
                await conn.commit()

                if cursor.rowcount == 0:
                    raise HTTPException(404, detail="Заказ не найден")

                return {"status": data['status']}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/api/orders/{order_id}/complete")
async def complete_order(order_id: int):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT file_path FROM `order` WHERE ID = %s",
                    (order_id,)
                )
                result = await cursor.fetchone()
                file_path = result[0] if result else None

                await cursor.execute(
                    "UPDATE `order` SET status = 'выдан' WHERE ID = %s",
                    (order_id,)
                )
                await conn.commit()

                if file_path:
                    full_path = os.path.join(UPLOAD_FOLDER, file_path)
                    try:
                        if os.path.exists(full_path):
                            await asyncio.to_thread(os.remove, full_path)
                    except Exception as e:
                        logging.error(f"Ошибка удаления файла: {str(e)}")

                return JSONResponse(content={"status": "выдан"})
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/api/orders")
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
        content = await file.read()
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(content)

        async with app.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()
                try:
                    await cursor.execute(
                        """INSERT INTO `order` 
                        (ID_shop, price, note, con_code, color, status, 
                         user_id, pages, file_extension, file_path) 
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            ID_shop,
                            price,
                            note,
                            con_code,
                            color,
                            'получен',
                            user_id,
                            pages,
                            file_extension,
                            temp_filename
                        )
                    )
                    order_id = cursor.lastrowid

                    new_filename = f"order_{order_id}{os.path.splitext(temp_filename)[1]}"
                    new_path = os.path.join(UPLOAD_FOLDER, new_filename)

                    if os.path.exists(temp_path):
                        await asyncio.to_thread(os.rename, temp_path, new_path)
                    else:
                        raise HTTPException(500, detail="Temp file not found")

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
                    raise e
    except Exception as e:
        logging.error(f"API Error: {str(e)}")
        raise HTTPException(500, detail=str(e))


@app.get("/api/shops")
async def get_shops():
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT name, ID_shop, address FROM shop")
                shops = await cursor.fetchall()
                return JSONResponse(content=shops)
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/shops/{shop_name}")
async def get_shop(shop_name: str):
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address, price_bw, price_cl FROM shop WHERE name = %s",
                    (shop_name,)
                )
                shop = await cursor.fetchone()
                return shop if shop else JSONResponse(status_code=404, content={"error": "Магазин не найден"})
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/shop/password")
async def get_shop_passwords():
    try:
        async with app.db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT password FROM shop")
                passwords = [row['password'] for row in await cursor.fetchall()]
                return passwords

    except Exception as e:
        logging.error(f"Error getting shop passwords: {str(e)}")
        raise HTTPException(500, detail=str(e))


@app.get("/api/files/{filename}")
async def get_file(filename: str):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        logging.error(f"File {filename} not found in {UPLOAD_FOLDER}")
        raise HTTPException(404, detail="File not found")
    return FileResponse(file_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)



# import os
# import uuid
# import logging
# from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, Body
# from fastapi.responses import JSONResponse, FileResponse
# from fastapi.staticfiles import StaticFiles
# import aiomysql
# from contextlib import asynccontextmanager
# from pydantic import BaseModel
# from typing import Optional, List
#
# logging.basicConfig(
#     level=logging.DEBUG,
#     filename='api.log',
#     format='%(asctime)s - %(levelname)s - %(message)s'
# )
#
#
# class OrderUpdate(BaseModel):
#     status: Optional[str] = None
#
#
# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     app.db_pool = await aiomysql.create_pool(
#         host='localhost',
#         user='root',
#         password='Qwerty123',
#         db='send_to_print',
#         auth_plugin='mysql_native_password',
#         minsize=5,
#         maxsize=20
#     )
#     yield
#     app.db_pool.close()
#     await app.db_pool.wait_closed()
#
#
# app = FastAPI(lifespan=lifespan)
# UPLOAD_FOLDER = os.path.abspath('uploads')
# os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")
#
#
# # --------------------- Orders ---------------------
# @app.get("/orders")
# async def get_orders(status: List[str] = Query(..., alias="status[]")):
#     try:
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor(aiomysql.DictCursor) as cursor:
#                 placeholders = ','.join(['%s'] * len(status))
#                 await cursor.execute(
#                     f"SELECT * FROM `order` WHERE status IN ({placeholders})",
#                     status
#                 )
#                 orders = await cursor.fetchall()
#                 for order in orders:
#                     order['price'] = float(order['price'])
#                 return orders
#     except Exception as e:
#         raise HTTPException(500, detail=str(e))
#
#
# @app.post("/order")
# async def update_order_status(
#         id: int = Query(...),
#         data: dict = Body(...)
# ):
#     try:
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor() as cursor:
#                 await cursor.execute(
#                     "UPDATE `order` SET status = %s WHERE ID = %s",
#                     (data['status'], id))
#                 await conn.commit()
#                 return {"status": "success"}
#     except Exception as e:
#         raise HTTPException(500, detail=str(e))
#
#
# # --------------------- Complete Order ---------------------
# @app.post("/api/orders/{order_id}/complete")
# async def complete_order(order_id: int):
#     try:
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor() as cursor:
#                 # Get file path
#                 await cursor.execute(
#                     "SELECT file_path FROM `order` WHERE ID = %s",
#                     (order_id,)
#                 )
#                 result = await cursor.fetchone()
#                 file_path = result[0] if result else None
#
#                 # Update status
#                 await cursor.execute(
#                     "UPDATE `order` SET status = 'выдан' WHERE ID = %s",
#                     (order_id,)
#                 )
#                 await conn.commit()
#
#                 # Delete file
#                 if file_path:
#                     full_path = os.path.join(UPLOAD_FOLDER, file_path)
#                     if os.path.exists(full_path):
#                         try:
#                             os.remove(full_path)
#                         except Exception as e:
#                             logging.error(f"File delete error: {str(e)}")
#
#                 return JSONResponse(content={"status": "выдан"})
#     except Exception as e:
#         raise HTTPException(500, detail=str(e))
#
#
# # --------------------- Create Order ---------------------
# @app.post("/api/orders")
# async def create_order(
#         file: UploadFile = File(...),
#         ID_shop: int = Form(...),
#         price: float = Form(...),
#         pages: int = Form(...),
#         color: str = Form(...),
#         user_id: str = Form(...),
#         note: str = Form(''),
#         con_code: int = Form(...),
#         file_extension: str = Form(...)
# ):
#     temp_filename = f"temp_{uuid.uuid4()}{os.path.splitext(file.filename)[1]}"
#     temp_path = os.path.join(UPLOAD_FOLDER, temp_filename)
#
#     try:
#         content = await file.read()
#         with open(temp_path, 'wb') as f:
#             f.write(content)
#
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor() as cursor:
#                 await conn.begin()
#                 try:
#                     await cursor.execute(
#                         """INSERT INTO `order`
#                         (ID_shop, price, note, con_code, color, status,
#                          user_id, pages, file_extension, file_path)
#                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
#                         (
#                             ID_shop,
#                             price,
#                             note,
#                             con_code,
#                             color,
#                             'получен',
#                             user_id,
#                             pages,
#                             file_extension,
#                             temp_filename
#                         )
#                     )
#                     order_id = cursor.lastrowid
#
#                     new_filename = f"order_{order_id}{os.path.splitext(temp_filename)[1]}"
#                     new_path = os.path.join(UPLOAD_FOLDER, new_filename)
#                     os.rename(temp_path, new_path)
#
#                     await cursor.execute(
#                         "UPDATE `order` SET file_path = %s WHERE ID = %s",
#                         (new_filename, order_id)
#                     )
#                     await conn.commit()
#                     return JSONResponse(
#                         content={"order_id": order_id, "con_code": con_code},
#                         status_code=201
#                     )
#                 except aiomysql.IntegrityError:
#                     await conn.rollback()
#                     os.remove(temp_path)
#                     raise HTTPException(400, "Confirmation code duplicate")
#                 except Exception as e:
#                     await conn.rollback()
#                     os.remove(temp_path)
#                     raise e
#     except Exception as e:
#         logging.error(f"API Error: {str(e)}")
#         raise HTTPException(500, detail=str(e))
#
#
# # --------------------- Shops ---------------------
# @app.get("/api/shops")
# async def get_shops():
#     try:
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor(aiomysql.DictCursor) as cursor:
#                 await cursor.execute("SELECT name, ID_shop, address FROM shop")
#                 shops = await cursor.fetchall()
#                 return JSONResponse(content=shops)
#     except Exception as e:
#         raise HTTPException(500, detail=str(e))
#
#
# @app.get("/api/shops/{shop_name}")
# async def get_shop(shop_name: str):
#     try:
#         async with app.db_pool.acquire() as conn:
#             async with conn.cursor(aiomysql.DictCursor) as cursor:
#                 await cursor.execute(
#                     "SELECT name, ID_shop, address, price_bw, price_cl FROM shop WHERE name = %s",
#                     (shop_name,)
#                 )
#                 shop = await cursor.fetchone()
#                 return shop if shop else JSONResponse(status_code=404, content={"error": "Shop not found"})
#     except Exception as e:
#         raise HTTPException(500, detail=str(e))
#
#
# # --------------------- Files ---------------------
# @app.get("/api/files/{filename}")
# async def get_file(filename: str):
#     file_path = os.path.join(UPLOAD_FOLDER, filename)
#     if not os.path.exists(file_path):
#         raise HTTPException(404, detail="File not found")
#     return FileResponse(file_path)
#
#
# if __name__ == "__main__":
#     import uvicorn
#
#     uvicorn.run(app, host="0.0.0.0", port=5000)