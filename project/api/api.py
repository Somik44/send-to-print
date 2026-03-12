import os
import uuid
import logging
import aiofiles
import traceback
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, UploadFile, Form, File, Query, Depends, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import aiomysql
import jwt
from decimal import Decimal
from urllib.parse import unquote
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.extension import Limiter

# Логирование
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
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "api.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf8",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["default"], "level": "INFO"},
    },
}

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS"))

security = HTTPBearer()

# Настройка лимитера для защиты от DDoS
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],  # Общий лимит для всех эндпоинтов
    storage_uri="memory://",  # Используем память для хранения счетчиков (можно заменить на redis://)
)

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# Database connection
async def get_db():
    return await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
        autocommit=False,
        cursorclass=aiomysql.DictCursor
    )

# JWT функции
class TokenData(BaseModel):
    shop_id: int
    exp: datetime


class ShopCreate(BaseModel):
    name: str
    address: str
    w_hours: str
    password: str


async def create_access_token(shop_data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "shop_id": shop_data['ID_shop'],
        "shop_name": shop_data['name'],
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenData:
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        shop_id = payload.get("shop_id")
        if shop_id is None:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return TokenData(shop_id=shop_id, exp=datetime.fromtimestamp(payload['exp'], tz=timezone.utc))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# Эндпоинты аутентификации с защитой
@app.post("/auth/login")
@limiter.limit("10/minute")  # Ограничиваем количество попыток входа
async def shop_login(
    request: Request,
    password_hash: str = Form(...)
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    "SELECT ID_shop, name, address FROM shop WHERE password = %s",
                    (password_hash,)
                )
                shop = await cursor.fetchone()
                if not shop:
                    raise HTTPException(status_code=401, detail="Invalid credentials")

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
        raise
    except Exception as e:
        logging.error(f"Login error: {str(e)}")
        raise HTTPException(status_code=500, detail="Authentication error")


@app.get("/auth/verify")
@limiter.limit("30/minute")
async def verify_token_endpoint(
    request: Request,
    current_shop: TokenData = Depends(verify_token)
):
    return {"valid": True, "shop_id": current_shop.shop_id, "expires_at": current_shop.exp.isoformat()}


# Эндпоинты для заказов с защитой
@app.get("/orders", response_model=List[dict])
@limiter.limit("10/minute")
async def get_orders(
    request: Request,
    status: List[str] = Query(..., title="Статусы заказов"),
    shop_id: Optional[int] = Query(None),
    current_shop: TokenData = Depends(verify_token)
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                placeholders = ",".join(["%s"] * len(status))
                query = f"SELECT ID, file_path, status FROM `order` WHERE status IN ({placeholders})"
                params = status.copy()

                if shop_id is not None:
                    query += " AND ID_shop = %s"
                    params.append(shop_id)
                else:
                    query += " AND ID_shop = %s"
                    params.append(current_shop.shop_id)

                await cursor.execute(query, params)
                return await cursor.fetchall()
    except Exception as e:
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.post("/orders/{order_id}/complete")
@limiter.limit("30/minute")
async def complete_order(
    request: Request,
    order_id: int,
    current_shop: TokenData = Depends(verify_token)
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await conn.begin()

                # Получаем информацию о заказе
                await cursor.execute(
                    "SELECT file_path FROM `order` WHERE ID = %s AND ID_shop = %s FOR UPDATE",
                    (order_id, current_shop.shop_id)
                )
                order = await cursor.fetchone()
                if not order:
                    await conn.rollback()
                    raise HTTPException(404, detail="Order not found")

                # Удаляем файл
                file_path = os.path.join(UPLOAD_FOLDER, order['file_path'])
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logging.info(f"Файл заказа {order_id} удалён")
                    except Exception as e:
                        logging.error(f"Ошибка удаления файла: {str(e)}")

                # Обновляем статус
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
        logging.error(f"Error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")


@app.get("/orders/count/{user_id}")
@limiter.limit("60/minute")
async def get_active_orders_count(
    request: Request,
    user_id: int
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT COUNT(*) as cnt
                    FROM `order`
                    WHERE user_id = %s AND status = 'received'
                """, (user_id,))

                result = await cursor.fetchone()
                return {"active_orders": result['cnt']}
    except Exception as e:
        logging.error(f"Count error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.get("/orders/user/{user_id}")
@limiter.limit("40/minute")
async def get_user_orders(
    request: Request,
    user_id: int
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT o.ID, o.user_file_name, s.name AS shop_name, s.address,
                           CONVERT_TZ(o.created_at, @@session.time_zone, '+00:00') AS created_at
                    FROM `order` o
                    JOIN shop s ON o.ID_shop = s.ID_shop
                    WHERE o.user_id = %s AND o.status = 'received'
                    ORDER BY o.ID DESC
                """, (user_id,))
                orders = await cursor.fetchall()
                return orders or []
    except Exception:
        logging.error(f"User orders error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.post("/orders")
@limiter.limit("20/minute")  # Ограничиваем создание новых заказов
async def create_order(
    request: Request,
    file: UploadFile = File(...),
    ID_shop: int = Form(...),
    user_id: str = Form(...)
):
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                # Вставляем запись о заказе
                decoded_filename = unquote(file.filename).strip()

                # ограничим длину
                decoded_filename = decoded_filename[:200]

                await cursor.execute("""
                    INSERT INTO `order` (ID_shop, user_id, file_path, user_file_name, status)
                    VALUES (%s, %s, %s, %s, 'received')
                """, (ID_shop, user_id, 'temp', decoded_filename))
                order_id = cursor.lastrowid

                # Генерируем имя файла и сохраняем
                ext = os.path.splitext(file.filename)[1]
                new_filename = f"order_{order_id}{ext}"
                new_path = os.path.join(UPLOAD_FOLDER, new_filename)

                async with aiofiles.open(new_path, 'wb') as f:
                    await f.write(await file.read())

                # Обновляем путь в БД
                await cursor.execute(
                    "UPDATE `order` SET file_path = %s WHERE ID = %s",
                    (new_filename, order_id)
                )
                await conn.commit()
                return JSONResponse(content={"order_id": order_id}, status_code=201)
    except Exception as e:
        if 'new_path' in locals() and os.path.exists(new_path):
            os.remove(new_path)
        logging.error(f"Order creation error: {traceback.format_exc()}")
        raise HTTPException(500, detail=str(e))


# Защищённый доступ к файлам
@app.get("/files/{filename}")
@limiter.limit("30/minute")
async def get_file(
    request: Request,
    filename: str,
    current_shop: TokenData = Depends(verify_token)
):
    try:
        # Проверяем, что файл принадлежит заказу текущей точки
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("""
                    SELECT o.ID_shop FROM `order` o
                    WHERE o.file_path = %s AND o.ID_shop = %s
                """, (filename, current_shop.shop_id))
                order = await cursor.fetchone()
                if not order:
                    raise HTTPException(403, detail="Access denied")

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(file_path):
            raise HTTPException(404, detail="File not found")
        return FileResponse(file_path)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"File access error: {traceback.format_exc()}")
        raise HTTPException(500, detail="Server error")


@app.post("/shops", status_code=201)
@limiter.limit("10/minute")  # Ограничиваем создание новых магазинов
async def create_shop(
    request: Request,
    shop: ShopCreate
):
    """Создание нового магазина (доступно без авторизации для админ-приложения)"""
    try:
        async with await get_db() as conn:
            async with conn.cursor() as cursor:
                # Проверяем уникальность пароля (хеша)
                await cursor.execute("SELECT ID_shop FROM shop WHERE password = %s", (shop.password,))
                existing = await cursor.fetchone()
                if existing:
                    raise HTTPException(status_code=409, detail="Shop with this password already exists")

                # Вставляем нового магазина (без цен)
                await cursor.execute("""
                    INSERT INTO shop (name, address, w_hours, password)
                    VALUES (%s, %s, %s, %s)
                """, (shop.name, shop.address, shop.w_hours, shop.password))
                await conn.commit()
                return {"message": "Shop created successfully", "id": cursor.lastrowid}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error creating shop: {traceback.format_exc()}")
        raise HTTPException(500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST")
    port = int(os.getenv("API_PORT"))
    # uvicorn.run(app, host=host, port=port, log_config=LOGGING_CONFIG)