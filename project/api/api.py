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
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, WebSocket, Depends, Header
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
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
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from yookassa import Configuration, Payment

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

TELEGRAM_BOT_URL = "https://t.me/print_there_bot"

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

MASTER_KEY = os.getenv("MASTER_KEY")
if not MASTER_KEY:
    raise ValueError("MASTER_KEY not set")

cipher = Fernet(MASTER_KEY.encode())

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
if not ADMIN_API_KEY:
    raise ValueError("ADMIN_API_KEY is not set in the environment file!")

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS"))

API_URL = os.getenv("API_URL")
security = HTTPBearer()


class TokenData(BaseModel):
    shop_id: int
    exp: datetime


# Изменение в POST /shops
class ShopCreate(BaseModel):
    name: str
    address: str
    w_hours: str
    price_bw: float
    price_cl: float
    password: str
    franchise_id: int

    # Новые модели
class ShopUpdate(BaseModel):
        name: Optional[str] = None
        address: Optional[str] = None
        w_hours: Optional[str] = None
        price_bw: Optional[float] = None
        price_cl: Optional[float] = None
        password: Optional[str] = None
        franchise_id: Optional[int] = None

class FranchiseOut(BaseModel):
    id: int
    name: str


class OrderUpdate(BaseModel):
    status: Optional[str] = None


class FranchiseCreate(BaseModel):
    name: str
    yk_shop_id: str
    yk_secret_key: str


class PaymentCreateRequest(BaseModel):
    order_id: int


app = FastAPI()
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


# Database configuration
async def get_db():
    return await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
        autocommit=False,
        cursorclass=aiomysql.DictCursor
    )


async def notify_bot(order_id: int, status: str):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT o.user_id, o.ID, s.address, o.con_code
                    FROM `order` o
                    JOIN shop s ON o.ID_shop = s.ID_shop
                    WHERE o.ID = %s
                """, (order_id,))
                data = await cursor.fetchone()

        if not data:
            logging.warning(f"Could not find order data for notification. Order ID: {order_id}")
            return

        bot_websocket_url = "ws://localhost:8001"

        logging.info(f"Connecting to bot WebSocket at {bot_websocket_url}...")
        async with websockets.connect(bot_websocket_url) as ws:
            payload = {
                "type": "status_update",
                "status": status,
                "user_id": data['user_id'],
                "order_id": data['ID'],
                "address": data['address'],
                "con_code": data['con_code']
            }
            await ws.send(json.dumps(payload))
            logging.info(f"Sent status '{status}' for order {order_id} to user {data['user_id']}")

    except ConnectionRefusedError:
        logging.error(f"WebSocket connection refused. Is the bot's WebSocket server running at {bot_websocket_url}?")
    except Exception as e:
        logging.error(f"WebSocket notification error for order {order_id}: {traceback.format_exc()}")


# Helper functions
def decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def encrypt_value(value: str) -> str:
    return cipher.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    return cipher.decrypt(value.encode()).decode()


async def verify_admin_key(x_admin_key: str = Header(None)):
    """Проверяет наличие и правильность секретного админского ключа."""
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing Admin API Key")


async def get_franchise_credentials(franchise_id: int):
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT yk_shop_id, yk_secret_key
                FROM franchise
                WHERE id = %s AND is_active = 1
            """, (franchise_id,))

            data = await cursor.fetchone()

            if not data:
                raise HTTPException(404, detail="Franchise not found or inactive")

            try:
                decrypted_secret = decrypt_value(data['yk_secret_key'])
            except Exception:
                logging.error("Failed to decrypt YooKassa secret key")
                raise HTTPException(500, detail="Decryption error")

            return {
                "shop_id": data['yk_shop_id'],
                "secret_key": decrypted_secret
            }


async def get_franchise_id_by_shop(shop_id: int) -> int:
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT franchise_id
                FROM shop
                WHERE ID_shop = %s AND is_active = 1
            """, (shop_id,))

            data = await cursor.fetchone()

            if not data or not data['franchise_id']:
                raise HTTPException(404, detail="Shop has no franchise assigned")

            return data['franchise_id']


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
        try:
            await notify_bot(order_id, 'ready')
        except Exception as e:
            logging.error(f"Failed to send 'ready' notification for order {order_id}: {e}")
        return {"status": "ready"}
    except Exception as e:
        logging.error(f"Error in mark_order_ready: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


@app.post("/orders/{order_id}/complete")
async def complete_order(order_id: int, current_shop: TokenData = Depends(verify_token)):
    """Завершить заказ (выдать клиенту)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
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

                user_id_for_notification = current['user_id']

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
        try:
            await notify_bot(order_id, 'completed')
        except Exception as e:
            logging.error(f"Failed to send 'completed' notification for order {order_id}: {e}")
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
                    ) VALUES (%s, %s, %s, %s, %s, 'created', %s, %s, %s, 'temp')
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


@app.post("/payments/create")
async def create_payment_endpoint(data: PaymentCreateRequest):
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT *
                FROM `order`
                WHERE ID = %s
            """, (data.order_id,))

            order = await cursor.fetchone()

            if not order:
                raise HTTPException(404, detail="Order not found")

            if order['status'] != 'created':
                raise HTTPException(400, detail=f"Cannot create payment for order with status '{order['status']}'")

    franchise_id = await get_franchise_id_by_shop(order['ID_shop'])
    creds = await get_franchise_credentials(franchise_id)

    Configuration.account_id = creds["shop_id"]
    Configuration.secret_key = creds["secret_key"]

    idempotence_key = str(uuid.uuid4())

    payment = Payment.create({
        "amount": {
            "value": str(order["price"]),
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": f"{API_URL}/payment-return"  # Этот URL сейчас не используется, но лучше его оставить
        },
        "capture": True,
        "description": f"Оплата заказа #{order['ID']}"
    }, idempotence_key)

    async with await get_db() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                UPDATE `order`
                SET payment_id = %s,
                    status = 'waiting_payment',
                    payment_status = %s,
                    idempotence_key = %s
                WHERE ID = %s
            """, (
                payment.id,
                payment.status,
                idempotence_key,
                data.order_id
            ))
            await conn.commit()

    logging.info(f"Created payment {payment.id} for order {data.order_id}. Status: waiting_payment")

    return {
        "confirmation_url": payment.confirmation.confirmation_url
    }


@app.post("/shops", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_shop(shop: ShopCreate):
    """Создание нового магазина (доступно без авторизации для админ-приложения)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                # Проверяем уникальность пароля (хеша)
                await cursor.execute("SELECT ID_shop FROM shop WHERE password = %s", (shop.password,))
                existing = await cursor.fetchone()
                if existing:
                    raise HTTPException(status_code=409, detail="Shop with this password already exists")

                # Вставляем нового магазина
                await cursor.execute("""
                    INSERT INTO shop (name, address, w_hours, price_bw, price_cl, password, franchise_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                shop.name, shop.address, shop.w_hours, shop.price_bw, shop.price_cl, shop.password, shop.franchise_id))
                await conn.commit()
                return {"message": "Shop created successfully", "id": cursor.lastrowid}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error creating shop: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")

@app.get("/franchise", response_model=List[FranchiseOut], dependencies=[Depends(verify_admin_key)])
async def get_franchises():
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT id, name FROM franchise WHERE is_active = 1")
            franchises = await cursor.fetchall()
            return franchises

# Эндпоинт для получения одного магазина (без пароля)
@app.get("/shops/{shop_id}", dependencies=[Depends(verify_admin_key)])
async def get_shop_by_id(shop_id: int):
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT ID_shop, name, address, w_hours, price_bw, price_cl, franchise_id
                FROM shop WHERE ID_shop = %s
            """, (shop_id,))
            shop = await cursor.fetchone()
            if not shop:
                raise HTTPException(404, detail="Shop not found")
            return shop

# Эндпоинт для обновления магазина (PATCH)
@app.patch("/shops/{shop_id}", dependencies=[Depends(verify_admin_key)])
async def update_shop(shop_id: int, update_data: ShopUpdate):
    async with await get_db() as conn:
        async with conn.cursor() as cursor:
            # Проверяем существование магазина
            await cursor.execute("SELECT ID_shop FROM shop WHERE ID_shop = %s", (shop_id,))
            if not await cursor.fetchone():
                raise HTTPException(404, detail="Shop not found")

            # Формируем динамический запрос на обновление
            fields = []
            values = []
            if update_data.name is not None:
                fields.append("name = %s")
                values.append(update_data.name)
            if update_data.address is not None:
                fields.append("address = %s")
                values.append(update_data.address)
            if update_data.w_hours is not None:
                fields.append("w_hours = %s")
                values.append(update_data.w_hours)
            if update_data.price_bw is not None:
                fields.append("price_bw = %s")
                values.append(update_data.price_bw)
            if update_data.price_cl is not None:
                fields.append("price_cl = %s")
                values.append(update_data.price_cl)
            if update_data.franchise_id is not None:
                fields.append("franchise_id = %s")
                values.append(update_data.franchise_id)
            if update_data.password is not None and update_data.password.strip():
                # Если передан новый пароль (не пустой), хешируем его
                fields.append("password = %s")
                values.append(update_data.password)  # предполагаем, что уже хеш

            if not fields:
                return {"message": "No fields to update"}

            query = f"UPDATE shop SET {', '.join(fields)} WHERE ID_shop = %s"
            values.append(shop_id)

            await cursor.execute(query, values)
            await conn.commit()

            return {"message": "Shop updated successfully"}


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


@app.post("/franchise", status_code=201, dependencies=[Depends(verify_admin_key)])
async def create_franchise(franchise: FranchiseCreate):
    try:
        encrypted_secret = encrypt_value(franchise.yk_secret_key)

        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO franchise (name, yk_shop_id, yk_secret_key)
                    VALUES (%s, %s, %s)
                """, (
                    franchise.name,
                    franchise.yk_shop_id,
                    encrypted_secret
                ))
                await conn.commit()

        return {"message": "Franchise created successfully"}

    except Exception as e:
        logging.error(f"Franchise creation error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


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


# payment endpoints
@app.get("/payments/check/{order_id}")
async def check_payment_status(order_id: int):
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT o.payment_id, o.status, o.ID_shop, o.con_code
                FROM `order` o
                WHERE o.ID = %s
            """, (order_id,))
            order = await cursor.fetchone()

    if not order or not order["payment_id"]:
        raise HTTPException(404, detail="Payment not found for this order")

    if order["status"] in ["paid", "canceled"]:
        return {"status": order["status"], "con_code": order.get("con_code")}

    franchise_id = await get_franchise_id_by_shop(order["ID_shop"])
    creds = await get_franchise_credentials(franchise_id)

    Configuration.account_id = creds["shop_id"]
    Configuration.secret_key = creds["secret_key"]

    payment = Payment.find_one(order["payment_id"])
    yookassa_status = payment.status

    if yookassa_status == "succeeded":
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE `order` SET status = 'paid', payment_status = 'succeeded', paid_at = NOW() "
                    "WHERE ID = %s AND status = 'waiting_payment'",
                    (order_id,)
                )
                await conn.commit()
        return {"status": "paid", "con_code": order.get("con_code")}

    elif yookassa_status == "canceled":
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE `order` SET status = 'canceled', payment_status = 'canceled' "
                    "WHERE ID = %s AND status = 'waiting_payment'",
                    (order_id,)
                )
                await conn.commit()
        return {"status": "canceled"}

    return {"status": "pending"}


@app.post("/orders/{order_id}/cancel-timeout")
async def cancel_order_due_to_timeout(order_id: int):
    """
    Безопасная отмена "зависшего" заказа по тайм-ауту из бота.
    """
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT status FROM `order` WHERE ID = %s", (order_id,)
                )
                order = await cursor.fetchone()

        if not order or order['status'] in ['paid', 'canceled']:
            return {"status": "ignored"}

        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE `order` SET status = 'canceled' WHERE ID = %s AND status = 'waiting_payment'",
                    (order_id,)
                )
                await conn.commit()

                if cursor.rowcount > 0:
                    logging.warning(f"Order {order_id} safely canceled by bot timeout.")
                    return {"status": "canceled"}
                else:
                    logging.info(f"Timeout cancellation for order {order_id} ignored, status was already paid.")
                    return {"status": "ignored"}

    except Exception as e:
        logging.error(f"Error in cancel_order_due_to_timeout for order {order_id}: {e}")
        raise HTTPException(500, detail="Internal server error")


@app.get("/payment-return", response_class=RedirectResponse)
async def payment_return():
    """
    Этот эндпоинт принимает пользователя от YooKassa после оплаты
    и немедленно перенаправляет его в Telegram-бота.
    """
    logging.info("User redirected back to the bot after payment attempt.")
    return RedirectResponse(url=TELEGRAM_BOT_URL, status_code=302)


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST")
    port = int(os.getenv("API_PORT"))
    uvicorn.run(app, host=host, port=port, log_config=LOGGING_CONFIG)