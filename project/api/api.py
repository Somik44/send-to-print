import os
import uuid
import logging
import aiofiles
import traceback
import json
import asyncio
import websockets
import aiomysql
import jwt
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, WebSocket, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
from decimal import Decimal
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect
from json import JSONDecodeError
from starlette.websockets import WebSocketState, WebSocketDisconnect
import aiohttp

# logging.basicConfig(
#     level=logging.DEBUG,
#     filename='api.log',
#     format='%(asctime)s - %(levelname)s - %(message)s'
# )

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": False,
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "api.log",
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "encoding": "utf8",
        },
        "access": {
            "formatter": "access",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "api.log",
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "encoding": "utf8",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

JWT_SECRET = "fQzoPHqr-PLxYFFIORSlOHSe8mhfv3M0WroY5a75i9VGR678LvPGcGh9AA7sa5arhepAnmHIVoBd8fIlsNw1KQ"
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

security = HTTPBearer()


class TokenData(BaseModel):
    shop_id: int
    exp: datetime


class OrderUpdate(BaseModel):
    status: Optional[str] = None


app = FastAPI()
# WS_URL = 'ws://tcp.cloudpub.ru:55000/bot'
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


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


# @app.websocket("/ws/notify")
# async def websocket_notify(websocket: WebSocket):
#     await websocket.accept()
#     try:
#         while True:
#             await websocket.receive_text()
#     except Exception as e:
#         logging.error(f"WebSocket connection closed: {str(e)}")
#
#
# async def notify_bot(order_id: int, status: str):
#     try:
#         # Используем существующее подключение через get_db()
#         async with await get_db() as conn:
#             async with conn.cursor(aiomysql.DictCursor) as cursor:
#                 await cursor.execute("""
#                     SELECT o.user_id, o.ID, s.address
#                     FROM `order` o
#                     JOIN shop s ON o.ID_shop = s.ID_shop
#                     WHERE o.ID = %s
#                 """, (order_id,))
#                 data = await cursor.fetchone()
#
#         # Исправляем адрес WebSocket на порт 8001
#         async with websockets.connect("ws://tcp.cloudpub.ru:55000") as ws:
#             await ws.send(json.dumps({
#                 "type": "status_update",
#                 "status": status,
#                 "user_id": data['user_id'],
#                 "order_id": data['ID'],
#                 "address": data['address']
#             }))
#     except Exception as e:
#         logging.error(f"WebSocket notification error: {traceback.format_exc()}")


# Helper functions
def decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


# JWT функции
async def create_access_token(shop_data: dict) -> str:
    expires_delta = timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    expire = datetime.now(timezone.utc) + expires_delta

    payload = {
        "shop_id": shop_data['ID_shop'],
        "shop_name": shop_data['name'],
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access"
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    logging.info(f"Created token for shop {shop_data['ID_shop']}, expires at: {expire}")
    return token


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenData:
    """Верификация JWT токена"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

        shop_id = payload.get("shop_id")
        if shop_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid token payload"
            )

        return TokenData(shop_id=shop_id, exp=datetime.fromtimestamp(payload['exp'], tz=timezone.utc))

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# Новые эндпоинты аутентификации
@app.post("/auth/login")
async def shop_login(password_hash: str = Form(...)):
    """Аутентификация точки и выдача токена"""
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT ID_shop, name, address FROM shop WHERE password = %s",
                    (password_hash,)
                )
                shop = await cursor.fetchone()

                if not shop:
                    # Просто возвращаем ошибку без логирования
                    raise HTTPException(status_code=401, detail="Invalid credentials")

                # Создаем токен
                access_token = await create_access_token(shop)

                return {
                    "access_token": access_token,
                    "token_type": "bearer",
                    "expires_in": ACCESS_TOKEN_EXPIRE_HOURS * 3600,
                    "shop_info": {
                        "ID_shop": shop['ID_shop'],
                        "name": shop['name'],
                        "address": shop['address']
                    }
                }

    except HTTPException:
        # Пробрасываем HTTP исключения без логирования
        raise
    except Exception as e:
        # Логируем только для админа, но не показываем пользователю
        logging.error(f"Login error: {str(e)}")
        raise HTTPException(status_code=500, detail="Authentication error")


@app.get("/auth/verify")
async def verify_token_endpoint(current_shop: TokenData = Depends(verify_token)):
    """Эндпоинт для проверки валидности токена"""
    return {
        "valid": True,
        "shop_id": current_shop.shop_id,
        "expires_at": current_shop.exp.isoformat()
    }


# Orders endpoints
@app.get("/orders", response_model=List[dict])
async def get_orders(
    status: List[str] = Query(..., title="Статусы заказов"),
    shop_id: Optional[int] = Query(None, title="ID магазина"),
    current_shop: TokenData = Depends(verify_token)
):
    """Получение заказов для авторизованной точки"""
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(status))
                query = f"SELECT * FROM `order` WHERE status IN ({placeholders})"
                params = status.copy()

                if shop_id is not None:
                    query += " AND ID_shop = %s"
                    params.append(shop_id)
                else:
                    # Если shop_id не указан, показываем только заказы текущей точки
                    query += " AND ID_shop = %s"
                    params.append(current_shop.shop_id)

                await cursor.execute(query, params)
                result = await cursor.fetchall()
                await conn.commit()
                return result

    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.post("/orders/{order_id}/ready")
async def mark_order_ready(order_id: int, current_shop: TokenData = Depends(verify_token)):
    """Пометить заказ как готовый"""
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()
                # Проверяем что заказ принадлежит точке
                await cursor.execute(
                    "SELECT user_id FROM `order` WHERE ID = %s AND ID_shop = %s FOR UPDATE",
                    (order_id, current_shop.shop_id)
                )
                current = await cursor.fetchone()

                if not current:
                    await conn.rollback()
                    raise HTTPException(404, detail="Order not found")

                await cursor.execute(
                    "UPDATE `order` SET status = 'ready' WHERE ID = %s AND ID_shop = %s",
                    (order_id, current_shop.shop_id)
                )
                await conn.commit()
                return {"status": "ready"}
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


@app.post("/orders/{order_id}/complete")
async def complete_order(order_id: int, current_shop: TokenData = Depends(verify_token)):
    """Завершить заказ (выдать клиенту)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await conn.begin()

                await cursor.execute(
                    """SELECT status, user_id, file_path 
                       FROM `order` 
                       WHERE ID = %s AND ID_shop = %s
                       FOR UPDATE""",
                    (order_id, current_shop.shop_id)
                )
                current = await cursor.fetchone()

                if not current:
                    await conn.rollback()
                    raise HTTPException(404, detail="Order not found")

                if current['status'] != 'ready':
                    await conn.rollback()
                    raise HTTPException(
                        400,
                        detail=f"Невозможно завершить заказ в статусе {current['status']}"
                    )

                file_path = os.path.join(UPLOAD_FOLDER, current['file_path'])
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logging.info(f"Файл заказа {order_id} удален: {file_path}")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла: {str(e)}")

                await cursor.execute(
                    "UPDATE `order` SET status = 'completed' WHERE ID = %s AND ID_shop = %s",
                    (order_id, current_shop.shop_id)
                )
                await conn.commit()
                return {"status": "completed"}

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
                    ) VALUES (%s, %s, %s, %s, %s, 'received', %s, %s, %s, 'temp')
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
    """Получение списка магазинов (публичный эндпоинт для бота)"""
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
    """Получение информации о магазине (публичный эндпоинт для бота)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address, w_hours, price_bw, price_cl FROM shop WHERE name = %s",
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
async def get_shop_by_password(password_hash: str, current_shop: TokenData = Depends(verify_token)):
    """Получение магазина по паролю (только для авторизованных точек)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT ID_shop, name, address FROM shop WHERE password = %s",
                    (password_hash,)
                )
                shop = await cursor.fetchone()
                if not shop:
                    return JSONResponse(
                        content={"detail": "Invalid password"},
                        status_code=401
                    )
                return shop
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


# Files endpoint
@app.get("/files/{filename}")
async def get_file(
        filename: str,
        current_shop: TokenData = Depends(verify_token)
):
    """Защищенный доступ к файлам - только для авторизованных точек"""
    try:
        # Проверяем, принадлежит ли файл заказа текущей точке
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT o.ID_shop 
                    FROM `order` o 
                    WHERE o.file_path = %s AND o.ID_shop = %s
                """, (filename, current_shop.shop_id))
                order = await cursor.fetchone()

                if not order:
                    raise HTTPException(
                        status_code=403,
                        detail="Access denied - file does not belong to your shop"
                    )

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            raise HTTPException(404, detail="File not found")

        return FileResponse(file_path)

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"File access error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000, log_config=LOGGING_CONFIG)