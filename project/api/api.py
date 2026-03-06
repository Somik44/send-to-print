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
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, WebSocket, Depends, Header, Request
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
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from contextlib import asynccontextmanager
import time

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

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
if not INTERNAL_API_KEY:
    raise ValueError("INTERNAL_API_KEY is not set in the environment file!")

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS"))
ORDER_TIMEOUT_MINUTES = int(os.getenv("ORDER_TIMEOUT_MINUTES"))
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
        is_active: Optional[int] = None


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


def rate_limit_key(request: Request):
    if request.headers.get("x-api-key") == INTERNAL_API_KEY:
        return "internal"
    return get_remote_address(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем существующие задачи
    task1 = asyncio.create_task(cancel_expired_orders())
    task2 = asyncio.create_task(clean_orphaned_files())  # новая задача
    yield
    task1.cancel()
    task2.cancel()
    try:
        await task1
        await task2
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)
limiter = Limiter(key_func=rate_limit_key)

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


async def notify_bot(order_id: int, status: str):
    """Отправляет уведомление об изменении статуса заказа в WebSocket-сервер бота."""
    try:
        # Получаем данные заказа
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
            logging.warning(f"Order {order_id} not found for notification")
            return

        # Подключаемся к WebSocket-серверу бота
        async with websockets.connect("ws://localhost:8001") as ws:
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
        logging.error(f"WebSocket connection refused to ws://localhost:8001. Is the bot's WebSocket server running?")
    except Exception as e:
        logging.error(f"WebSocket notification error for order {order_id}: {traceback.format_exc()}")


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


async def cancel_order(order_id: int, payment_id: Optional[str], shop_id: int, conn, cursor, notify_user: bool = True):
    """
    Отменяет заказ. Если есть payment_id, синхронизируется с ЮKassa:
    - Если платёж успешен → переводит заказ в paid.
    - Если платёж отменён → просто обновляет статус.
    - Иначе пытается отменить платёж.
    Отправляет соответствующее уведомление пользователю.
    """
    try:
        final_status = 'canceled'
        notification_status = 'expired'  # по умолчанию для таймаута

        if payment_id:
            # Получаем ключи франшизы для этого магазина
            await cursor.execute("""
                SELECT f.yk_shop_id, f.yk_secret_key
                FROM shop s
                JOIN franchise f ON s.franchise_id = f.id
                WHERE s.ID_shop = %s
            """, (shop_id,))
            franchise = await cursor.fetchone()
            if franchise:
                try:
                    secret_key = decrypt_value(franchise['yk_secret_key'])
                    Configuration.account_id = franchise['yk_shop_id']
                    Configuration.secret_key = secret_key

                    # Асинхронно получаем актуальный статус платежа из ЮKassa
                    payment = await asyncio.to_thread(Payment.find_one, payment_id)

                    if payment.status == 'succeeded':
                        # Платёж уже успешен → переводим заказ в paid
                        final_status = 'paid'
                        notification_status = 'paid'
                        await cursor.execute("""
                            UPDATE `order`
                            SET status = 'paid',
                                payment_status = 'succeeded',
                                paid_at = NOW()
                            WHERE ID = %s
                        """, (order_id,))
                        await conn.commit()
                        try:
                            await notify_bot(order_id, notification_status)
                        except Exception as e:
                            logging.error(f"Failed to send '{notification_status}' notification for order {order_id}: {e}")
                        return  # Завершаем, заказ обработан

                    elif payment.status == 'canceled':
                        # Платёж уже отменён, просто обновим БД
                        final_status = 'canceled'
                        notification_status = 'expired'
                    else:
                        # Платёж в другом статусе (например, waiting_for_capture) – пытаемся отменить
                        try:
                            await asyncio.to_thread(Payment.cancel, payment_id)
                            logging.info(f"Payment {payment_id} canceled in YooKassa")
                        except Exception as e:
                            logging.warning(f"YooKassa cancel failed: {e}")
                        # В любом случае ставим статус canceled
                        final_status = 'canceled'
                        notification_status = 'expired'
                except Exception as e:
                    logging.error(f"Error processing payment {payment_id}: {e}")
                    # При ошибке запроса к ЮKassa тоже отменяем заказ в БД
                    final_status = 'canceled'
                    notification_status = 'expired'
            else:
                # Франшиза не найдена – не можем проверить платёж, отменяем только в БД
                final_status = 'canceled'
                notification_status = 'expired'
        else:
            # Нет payment_id (заказ в статусе created) – просто отменяем
            final_status = 'canceled'
            notification_status = 'expired'

        # Если заказ не стал paid, отменяем в БД
        if final_status == 'canceled':
            await cursor.execute("""
                        UPDATE `order`
                        SET status = 'canceled', payment_status = 'canceled'
                        WHERE ID = %s
                    """, (order_id,))
            await conn.commit()

            # Получаем file_path для удаления
            await cursor.execute("SELECT file_path FROM `order` WHERE ID = %s", (order_id,))
            file_data = await cursor.fetchone()
            if file_data and file_data['file_path']:
                try:
                    await delete_order_file(order_id, file_data['file_path'])
                except Exception as e:
                    logging.error(f"Failed to delete file for order {order_id}: {e}")

            if notify_user:
                try:
                    await notify_bot(order_id, notification_status)
                except Exception as e:
                    logging.error(f"Failed to send '{notification_status}' notification for order {order_id}: {e}")
            logging.info(f"Order {order_id} canceled (notify_user={notify_user})")

    except Exception as e:
        logging.error(f"Error cancelling order {order_id}: {e}")
        await conn.rollback()


async def cancel_expired_orders():
    """Проверяет просроченные заказы каждую минуту."""
    while True:
        try:
            async with await get_db() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("""
                        SELECT ID, payment_id, ID_shop
                        FROM `order`
                        WHERE status = 'waiting_payment'
                          AND created_at < NOW() - INTERVAL %s MINUTE
                    """, (ORDER_TIMEOUT_MINUTES,))
                    expired_orders = await cursor.fetchall()

                    for order in expired_orders:
                        logging.info(f"Expired order {order['ID']} found, cancelling...")
                        await cancel_order(order['ID'], order['payment_id'], order['ID_shop'], conn, cursor)
                        await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"Error in cancel_expired_orders: {e}", exc_info=True)

        await asyncio.sleep(60)  # интервал проверки — 1 минута


async def delete_order_file(order_id: int, file_path: str) -> bool:
    """Удаляет файл заказа с диска, если он существует."""
    try:
        full_path = os.path.join(UPLOAD_FOLDER, file_path)
        if os.path.exists(full_path):
            os.remove(full_path)
            logging.info(f"Deleted file for order {order_id}: {file_path}")
            return True
        else:
            logging.warning(f"File for order {order_id} not found: {file_path}")
            return False
    except Exception as e:
        logging.error(f"Error deleting file for order {order_id}: {e}")
        return False


async def clean_orphaned_files():
    """Периодически удаляет файлы отменённых/завершённых заказов и старые временные файлы."""
    while True:
        try:
            # 1. Удаляем файлы отменённых и завершённых заказов
            async with await get_db() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("""
                        SELECT ID, file_path
                        FROM `order`
                        WHERE status IN ('canceled', 'completed')
                          AND file_path IS NOT NULL
                          AND file_path != ''
                    """)
                    orders = await cursor.fetchall()
                    for order in orders:
                        file_path = order['file_path']
                        full_path = os.path.join(UPLOAD_FOLDER, file_path)
                        if os.path.exists(full_path):
                            try:
                                os.remove(full_path)
                                logging.info(f"Cleaned orphaned order file: {file_path} for order {order['ID']}")
                            except Exception as e:
                                logging.error(f"Failed to clean order file {file_path}: {e}")

            # 2. Удаляем старые временные файлы (temp_*), старше 1 часа
            now = time.time()
            for filename in os.listdir(UPLOAD_FOLDER):
                if filename.startswith("temp_"):
                    file_path = os.path.join(UPLOAD_FOLDER, filename)
                    if os.path.isfile(file_path):
                        file_age = now - os.path.getmtime(file_path)
                        if file_age > 3600:  # 1 час
                            try:
                                os.remove(file_path)
                                logging.info(f"Deleted old temp file: {filename}")
                            except Exception as e:
                                logging.error(f"Failed to delete old temp file {filename}: {e}")
        except Exception as e:
            logging.error(f"Error in clean_orphaned_files: {e}", exc_info=True)

        await asyncio.sleep(24 * 60 * 60)  # раз в сутки


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


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests"}
    )


# Новые эндпоинты аутентификации
@app.post("/auth/login")
@limiter.limit("10/minute")
async def shop_login(request: Request, password_hash: str = Form(...)):
    """Аутентификация точки и выдача токена"""
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT ID_shop, name, address, is_active FROM shop WHERE password = %s",
                    (password_hash,)
                )
                shop = await cursor.fetchone()

                if not shop:
                    # Просто возвращаем ошибку без логирования
                    raise HTTPException(status_code=401, detail="Invalid credentials")

                # Проверяем, активна ли точка
                if not shop['is_active']:
                    raise HTTPException(
                        status_code=403,
                        detail="Shop is inactive. Please contact administrator."
                    )

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
@limiter.limit("30/minute")
async def get_orders(
    request: Request,
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


@app.get("/orders/{order_id}")
async def get_order(order_id: int, x_api_key: str = Header(None)):
    if x_api_key != INTERNAL_API_KEY:
        raise HTTPException(403)
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT ID, status FROM `order` WHERE ID = %s", (order_id,))
            order = await cursor.fetchone()
            if not order:
                raise HTTPException(404, detail="Order not found")
            return order


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
        file_extension: str = Form(...),
        x_api_key: str = Header(None)
):
    if x_api_key != INTERNAL_API_KEY: raise HTTPException(403)
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
            "value": f"{order['price']:.2f}",
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{API_URL}/payment-return"
        },
        "metadata": {
            "order_id": str(order["ID"]),
            "shop_id": str(order["ID_shop"])
        },
        "description": f"Оплата заказа #{order['ID']}"
    }, idempotence_key)

    async with await get_db() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                UPDATE `order`
                SET payment_id = %s,
                    status = 'waiting_payment',
                    payment_status = %s,
                    idempotence_key = %s,
                    payment_amount = NULL
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


@app.post("/admin/shops", status_code=201, dependencies=[Depends(verify_admin_key)])
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


@app.get("/admin/franchise", response_model=List[FranchiseOut], dependencies=[Depends(verify_admin_key)])
async def get_franchises():
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT id, name FROM franchise WHERE is_active = 1")
            franchises = await cursor.fetchall()
            return franchises


# Эндпоинт для получения одного магазина (без пароля)
@app.get("/admin/shops/{shop_id}", dependencies=[Depends(verify_admin_key)])
async def get_shop_by_id(shop_id: int):
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("""
                SELECT ID_shop, name, address, w_hours, price_bw, price_cl, franchise_id, is_active
                FROM shop WHERE ID_shop = %s
            """, (shop_id,))
            shop = await cursor.fetchone()
            if not shop:
                raise HTTPException(404, detail="Shop not found")
            return shop


# Эндпоинт для обновления магазина (PATCH)
@app.patch("/admin/shops/{shop_id}", dependencies=[Depends(verify_admin_key)])
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
            if update_data.is_active is not None:
                fields.append("is_active = %s")
                values.append(update_data.is_active)
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


@app.get("/admin/shops", dependencies=[Depends(verify_admin_key)])
async def get_shops():
    """Получение списка магазинов (приватный эндпоинт для редактирования точек)"""
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


# Shops endpoints
@app.get("/shops")
@limiter.limit("50/minute")
async def get_shops(request: Request, x_api_key: str = Header(None)):
    """Получение списка магазинов (публичный эндпоинт для бота)"""
    if x_api_key != INTERNAL_API_KEY: raise HTTPException(403)
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT name, ID_shop, address FROM shop WHERE is_active = 1")
                shops = await cursor.fetchall()
                return shops or JSONResponse(
                    content={"message": "No shops found"},
                    status_code=404
                )
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.get("/shops/{shop_name}")
async def get_shop(shop_name: str, x_api_key: str = Header(None)):
    """Получение информации о магазине (публичный эндпоинт для бота)"""
    if x_api_key != INTERNAL_API_KEY: raise HTTPException(403)
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT name, ID_shop, address, w_hours, price_bw, price_cl FROM shop WHERE name = %s AND is_active = 1",
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


@app.post("/admin/franchise", status_code=201, dependencies=[Depends(verify_admin_key)])
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


@app.get("/users/all-ids")
async def get_all_user_ids(x_api_key: str = Header(None)):
    if not INTERNAL_API_KEY or x_api_key != INTERNAL_API_KEY:
        logging.warning(f"Unauthorized access attempt to user IDs. Key: {x_api_key}")
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        async with await get_db() as conn:
            # Используем DictCursor, так как он у тебя в get_db по умолчанию
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT DISTINCT user_id FROM `order` WHERE user_id IS NOT NULL")
                result = await cursor.fetchall()

                # Обращаемся по имени колонки, а не по индексу
                return [row['user_id'] for row in result]

    except Exception as e:
        logging.error(f"DB Error in get_all_user_ids: {traceback.format_exc()}")
        raise HTTPException(500, detail="Database error")


# Files endpoint
@app.get("/files/{filename}")
@limiter.limit("10/minute")
async def get_file(
        request: Request,
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
@app.post("/yookassa/webhook")
async def yookassa_webhook(request: Request):
    try:
        event_json = await request.json()
        notification_object = event_json.get("object", {})
        payment_id = notification_object.get("id")
        event = event_json.get("event")  # Тип события, пригодится для фильтрации

        if not payment_id:
            return {"status": "ok"}

        # Получаем соединение с БД
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Исправленный запрос: связываем order → shop → franchise
                await cursor.execute("""
                    SELECT o.ID, o.ID_shop, o.status, o.payment_status,
                           f.yk_shop_id, f.yk_secret_key
                    FROM `order` o
                    JOIN shop s ON o.ID_shop = s.ID_shop
                    JOIN franchise f ON s.franchise_id = f.id
                    WHERE o.payment_id = %s
                """, (payment_id,))
                order = await cursor.fetchone()

                if not order:
                    logging.warning(f"Order with payment_id {payment_id} not found")
                    return {"status": "ok"}

                # Проверяем, не обработан ли уже этот платёж
                if order['payment_status'] == 'succeeded':
                    logging.info(f"Payment {payment_id} already processed")
                    return {"status": "ok"}

                # Конфигурируем SDK ЮKassa
                try:
                    secret_key = decrypt_value(order['yk_secret_key'])  # используем существующую функцию
                except Exception as e:
                    logging.error(f"Failed to decrypt secret key: {e}")
                    return {"status": "error"}, 500

                Configuration.account_id = order['yk_shop_id']
                Configuration.secret_key = secret_key

                # Проверяем статус платежа напрямую в ЮKassa
                payment_from_yk = Payment.find_one(payment_id)

                # Обрабатываем только событие payment.succeeded
                if payment_from_yk.status == "succeeded":
                    # Получаем фактическую сумму из платежа (всегда передаётся в YooKassa)
                    amount_value = payment_from_yk.amount.value
                    if amount_value:
                        payment_amount = Decimal(amount_value)
                    else:
                        payment_amount = None
                        logging.warning(f"Payment {payment_id} has no amount value")

                    # Проверяем, что заказ ещё в статусе waiting_payment (идемпотентность)
                    if order['status'] == 'waiting_payment':
                        await cursor.execute("""
                            UPDATE `order`
                            SET status = 'paid',
                                payment_status = 'succeeded',
                                paid_at = NOW(),
                                payment_amount = %s
                            WHERE ID = %s AND status = 'waiting_payment'
                        """, (payment_amount, order['ID']))
                        await conn.commit()
                        logging.info(f"Order {order['ID']} marked as PAID with amount {payment_amount}")

                        # Отправляем уведомления
                        try:
                            await notify_bot(order['ID'], "paid")
                        except Exception as e:
                            logging.error(f"Failed to send 'paid' notification for order {order_id}: {e}")

                elif payment_from_yk.status == "canceled":
                    if order['status'] == 'waiting_payment':
                        await cursor.execute("""
                            UPDATE `order`
                            SET status = 'canceled',
                                payment_status = 'canceled'
                            WHERE ID = %s
                        """, (order['ID'],))
                        await conn.commit()
                        try:
                            await notify_bot(order['ID'], "canceled")
                        except Exception as e:
                            logging.error(f"Failed to send 'canceled' notification for order {order_id}: {e}")

        return {"status": "ok"}

    except Exception as e:
        logging.error(f"Webhook error: {e}", exc_info=True)
        return {"status": "ok"}  # Всегда возвращаем 200 для ЮKassa


@app.post("/orders/{order_id}/cancel-timeout")
async def cancel_order_due_to_timeout(order_id: int):
    """
    Эндпоинт для отмены заказа по таймауту (вызывается вручную или фоновым процессом).
    Использует общую функцию cancel_order, которая синхронизируется с ЮKassa и отправляет уведомления.
    """
    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            # Получаем данные заказа
            await cursor.execute("""
                SELECT payment_id, ID_shop, status
                FROM `order`
                WHERE ID = %s
            """, (order_id,))
            order = await cursor.fetchone()

            if not order:
                raise HTTPException(404, detail="Order not found")

            # Если заказ уже не в ожидании оплаты, ничего не делаем (уже обработан)
            if order['status'] != 'waiting_payment':
                return {"status": "already_processed", "current_status": order['status']}

            # Вызываем общую функцию отмены заказа
            await cancel_order(
                order_id=order_id,
                payment_id=order['payment_id'],
                shop_id=order['ID_shop'],
                conn=conn,
                cursor=cursor
            )

    return {"status": "canceled"}


@app.get("/payment-return", response_class=RedirectResponse)
async def payment_return():
    """
    Этот эндпоинт принимает пользователя от YooKassa после оплаты
    и немедленно перенаправляет его в Telegram-бота.
    """
    logging.info("User redirected back to the bot after payment attempt.")
    return RedirectResponse(url=TELEGRAM_BOT_URL, status_code=302)


@app.post("/admin/orders/{order_id}/cancel")
async def admin_cancel_order(order_id: int, x_api_key: str = Header(None)):
    """
    Эндпоинт для отмены заказа по запросу от бота (административный).
    Использует существующую логику cancel_order_due_to_timeout.
    """
    if not INTERNAL_API_KEY or x_api_key != INTERNAL_API_KEY:
        logging.warning(f"Unauthorized access attempt to user IDs. Key: {x_api_key}")
        raise HTTPException(status_code=403, detail="Access denied")

    async with await get_db() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT payment_id, ID_shop, status FROM `order` WHERE ID = %s", (order_id,))
            order = await cursor.fetchone()
            if not order:
                raise HTTPException(404, detail="Order not found")
            if order['status'] != 'waiting_payment':
                return {"status": "already_processed"}
            await cancel_order(order_id, order['payment_id'], order['ID_shop'], conn, cursor, notify_user=False)
    return {"status": "canceled"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST")
    port = int(os.getenv("API_PORT"))
    uvicorn.run(app, host=host, port=port, log_config=LOGGING_CONFIG)