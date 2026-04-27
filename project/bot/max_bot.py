import os
import logging
import asyncio
import aiohttp
import json
import uuid
import random
import traceback
import aiofiles
import magic
import tempfile
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from aiohttp import web
from maxapi import Bot, Dispatcher
from maxapi.context import MemoryContext, StatesGroup, State
from maxapi.types import MessageCreated, Command, MessageCallback, BotStarted
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import LinkButton, CallbackButton
from maxapi.methods.send_message import SendMessage
from maxapi.methods.delete_message import DeleteMessage
from maxapi.enums import TextFormat
from maxapi.enums.parse_mode import ParseMode
from maxapi.types.input_media import InputMediaBuffer
from PIL import Image
import io
from datetime import datetime, timezone
from utils import (
    download_file_from_url,
    get_page_count,
    is_file_safe,
    scan_file,
    MAX_FILE_SIZE,
    UPLOAD_FOLDER
)

# --------------------------
# Конфигурация и Логирование
# --------------------------
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = 'max_bot.log'
handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
handler.setFormatter(log_formatter)
handler.setLevel(logging.DEBUG)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

ADMIN_IDS_MAX = [int(id.strip()) for id in os.getenv("ADMIN_IDS_MAX", "").split(",") if id.strip()]
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
API_URL = os.getenv("API_URL")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")

bot = Bot(token=MAX_BOT_TOKEN)
dp = Dispatcher()

# Глобальные словари
active_orders = {}       # user_id -> order_id
payment_messages = {}    # user_id -> message_id
order_timers = {}        # user_id -> asyncio.Task
user_chat_mapping = {}   # user_id(str) -> chat_id(int)
processing_payment = set()  # защита от повторного нажатия «Оплатить»
processing_cancel = set()    # защита от повторного нажатия «Отменить оплату»
broadcast_data = {}  # user_id -> {'text': str, 'photos': list[str]}

# --------------------------
# Вспомогательные функции
# --------------------------
def get_user_id(event) -> str:
    """Извлекает user_id из message_created события."""
    if hasattr(event, 'message') and hasattr(event.message, 'sender'):
        return str(event.message.sender.user_id)
    if hasattr(event, 'user_id'):
        return str(event.user_id)
    if hasattr(event, 'sender') and hasattr(event.sender, 'user_id'):
        return str(event.sender.user_id)
    if hasattr(event, 'from_id'):
        return str(event.from_id)
    return None


def get_chat_id(event) -> int:
    if hasattr(event, 'chat_id'):
        return event.chat_id
    if hasattr(event, 'message') and hasattr(event.message, 'recipient'):
        return event.message.recipient.chat_id
    return None


def is_admin(user_id: str) -> bool:
    return int(user_id) in ADMIN_IDS_MAX


def update_chat_mapping(event):
    user_id = get_user_id(event)
    chat_id = get_chat_id(event)
    if user_id and chat_id:
        user_chat_mapping[user_id] = chat_id


async def send_message_to_user(user_id: str, text: str, attachments=None, format=None):
    chat_id = user_chat_mapping.get(user_id)
    if chat_id is None:
        try:
            chat_id = int(user_id)
        except ValueError:
            logging.error(f"Cannot determine chat_id for user {user_id}")
            return None
    try:
        msg = await SendMessage(
            bot,
            chat_id=chat_id,
            text=text,
            attachments=attachments,
            format=format
        ).fetch()
        return msg
    except Exception as e:
        logging.error(f"Failed to send message to user {user_id}: {e}")
        return None


# ---------- Онбординг через API ----------
async def _get_onboarding_status(user_id: str) -> dict:
    headers = {"x-api-key": INTERNAL_API_KEY}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{API_URL}/users/{user_id}/onboarding-status",
                params={"platform": "max"},
                headers=headers
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logging.error(f"onboarding-status request failed: {e}")
    return {}


async def _mark_welcomed(user_id: str):
    headers = {"x-api-key": INTERNAL_API_KEY}
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{API_URL}/users/{user_id}/onboarding-welcome",
            params={"platform": "max"}, headers=headers
        )


async def _mark_agreed(user_id: str):
    headers = {"x-api-key": INTERNAL_API_KEY}
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{API_URL}/users/{user_id}/onboarding-agree",
            params={"platform": "max"}, headers=headers
        )


async def ensure_onboarding(user_id: str) -> bool:
    status = await _get_onboarding_status(user_id)
    welcomed = status.get("welcomed", False)
    agreed = status.get("agreed", False)
    if not welcomed:
        await _mark_welcomed(user_id)
        await _send_welcome_and_agreement(user_id)
        return False
    if not agreed:
        await _request_agreement(user_id)
        return False
    return True


async def _send_welcome_and_agreement(user_id: str, name: str = None):
    if not name:
        name = "друг"
    text = (
        f"Привет, {name}! Рады приветствовать тебя на нашем сервисе по печати "
        f"документов в любое удобное время! Прежде чем начать, пожалуйста, ознакомьтесь с правилами и нажмите кнопку ниже.\n\n"
        "📚 <a href='https://disk.yandex.ru/d/Q-1xYZuSQFZNYA'>Документация сервиса Send to print and pick up!</a>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Согласен", payload="agree_terms"))
    await send_message_to_user(
        user_id,
        text=text,
        attachments=[builder.as_markup()], format=ParseMode.HTML)


async def _request_agreement(user_id: str):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Согласен", payload="agree_terms"))
    await send_message_to_user(
        user_id,
        "Чтобы пользоваться ботом, необходимо принять условия. Нажмите кнопку ниже.",
        [builder.as_markup()]
    )


# --------------------------
# Состояния (FSM)
# --------------------------
class Form(StatesGroup):
    shop_selection = State()
    file_processing = State()
    color_selection = State()
    comment = State()
    confirmation = State()
    admin_broadcast = State()
    confirm_broadcast = State()


# --------------------------
# Клавиатуры
# --------------------------
def get_main_kb(user_id: str = None):
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="🛒 Новый заказ", payload="new_order"))
    builder.row(CallbackButton(text="ℹ️ Помощь", payload="help"))
    if user_id and is_admin(user_id):
        builder.row(CallbackButton(text="📢 Рассылка", payload="broadcast"))
    return [builder.as_markup()]


def get_cancel_kb():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_shops_kb(shops):
    builder = InlineKeyboardBuilder()
    for shop in shops:
        builder.row(CallbackButton(text=shop['name'], payload=f"shop_{shop['ID_shop']}"))
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_colors_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="Черно-белая", payload="color_bw"),
        CallbackButton(text="Цветная", payload="color_cl")
    )
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_skip_kb():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="Без комментария", payload="skip_comment"))
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_confirm_kb(url=None):
    builder = InlineKeyboardBuilder()
    if url:
        builder.row(LinkButton(text="💳 Оплатить", url=url))
        builder.row(CallbackButton(text="❌ Отменить оплату", payload="reset_order"))
    else:
        builder.row(CallbackButton(text="✅ Подтвердить", payload="confirm_order"))
        builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_active_order_kb():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Продолжить текущий", payload="continue_current"))
    builder.row(CallbackButton(text="❌ Отменить и начать заново", payload="cancel_and_start"))
    return [builder.as_markup()]


def get_broadcast_ready_kb():
    """Клавиатура для режима накопления фото."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Готово", payload="broadcast_ready"))
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_broadcast_confirm_kb():
    """Клавиатура подтверждения рассылки."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="✅ Отправить", payload="send_broadcast"))
    builder.row(CallbackButton(text="❌ Отмена", payload="reset_order"))
    return [builder.as_markup()]


# --------------------------
# Загрузка фото в MAX
# --------------------------
def extract_photo_url(event: MessageCreated) -> str:
    """Извлекает URL самого большого изображения из сообщения MAX."""
    try:
        if hasattr(event.message, 'body') and event.message.body:
            attachments = getattr(event.message.body, 'attachments', [])
            for att in attachments:
                # Проверяем тип вложения
                att_type = getattr(att, 'type', '')
                if att_type == 'image':
                    payload = getattr(att, 'payload', None)
                    if payload:
                        url = getattr(payload, 'url', None)
                        if url:
                            return url
        # Резервный вариант – через словарь (если объект не распарсился)
        if hasattr(event.message, 'body') and isinstance(event.message.body, dict):
            for att in event.message.body.get('attachments', []):
                if att.get('type') == 'image':
                    return att.get('payload', {}).get('url', '')
    except Exception as e:
        logging.error(f"extract_photo_url error: {e}")
    return ""


# --------------------------
# Работа с файлами (без изменений)
# --------------------------
def extract_file_link(event: MessageCreated) -> str:
    try:
        if hasattr(event.message, 'model_dump'):
            msg_dict = event.message.model_dump()
        elif hasattr(event.message, 'dict'):
            msg_dict = event.message.dict()
        else:
            msg_dict = vars(event.message)
    except Exception:
        msg_dict = {}

    body = msg_dict.get('body', {})
    if isinstance(body, dict):
        attachments = body.get('attachments', [])
        if isinstance(attachments, list):
            for att in attachments:
                if isinstance(att, dict):
                    payload = att.get('payload', {})
                    url = payload.get('url') or att.get('url') or att.get('fileUrl') or att.get('link')
                    if isinstance(url, str) and url.startswith("http"):
                        return url

    def find_url_recursively(data):
        if isinstance(data, dict):
            url = data.get('url') or data.get('link') or data.get('fileUrl')
            if isinstance(url, str) and url.startswith("http"):
                return url
            for value in data.values():
                res = find_url_recursively(value)
                if res: return res
        elif isinstance(data, list):
            for item in data:
                res = find_url_recursively(item)
                if res: return res
        return ""

    return find_url_recursively(msg_dict)


async def download_and_detect_ext(url: str, user_id: str) -> tuple:
    file_content = await download_file_from_url(url)
    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError("Файл слишком большой. Максимальный размер — 20 МБ")

    mime = magic.from_buffer(file_content, mime=True)

    if mime == 'image/webp':
        img = Image.open(io.BytesIO(file_content))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=95)
        file_content = output.getvalue()
        mime = 'image/jpeg'
        file_ext = '.jpg'
    else:
        mime_to_ext = {
            'application/pdf': '.pdf',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'image/png': '.png',
            'image/jpeg': '.jpg',
            'image/webp': '.webp'
        }
        file_ext = mime_to_ext.get(mime)
        if not file_ext:
            raise ValueError(
                f"Неподдерживаемый формат файла (получен {mime}). Разрешены только PDF, DOC, DOCX, PNG, JPG, WEBP.")

    temp_name = f"temp_{uuid.uuid4()}{file_ext}"
    temp_path = os.path.join(UPLOAD_FOLDER, temp_name)

    async with aiofiles.open(temp_path, 'wb') as f:
        await f.write(file_content)

    return temp_path, file_ext, f"file_{user_id}{file_ext}"


async def has_active_order(user_id: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": INTERNAL_API_KEY}
            async with session.get(f"{API_URL}/users/{user_id}/active-order", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data is not None
    except Exception as e:
        logging.error(f"has_active_order error: {e}")
    return False


async def cancel_order_via_api(order_id: int):
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": INTERNAL_API_KEY}
            await session.post(f"{API_URL}/admin/orders/{order_id}/cancel", headers=headers)
            logging.info(f"Order {order_id} cancelled via API")
    except Exception as e:
        logging.error(f"Error cancelling order {order_id}: {e}")


async def reset_logic(user_id: str, context: MemoryContext, send_message=True, cancel_timer=True):
    if not user_id: return
    order_id = active_orders.pop(user_id, None)
    if order_id:
        await cancel_order_via_api(order_id)
    await delete_message_for_user(user_id)

    if cancel_timer:
        if user_id in order_timers:
            order_timers[user_id].cancel()
            del order_timers[user_id]
    else:
        # Если не нужно отменять (таймер истекает сам), просто удаляем запись
        if user_id in order_timers:
            del order_timers[user_id]

    data = await context.get_data()
    temp_file = data.get("temp_file")
    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except Exception:
            pass
    await context.clear()
    if send_message:
        await send_message_to_user(user_id, "❌ Заказ отменен", get_main_kb(user_id))


async def delete_message_for_user(user_id: str):
    if user_id in payment_messages:
        chat_id = user_chat_mapping.get(user_id)
        if chat_id:
            try:
                await DeleteMessage(bot, str(payment_messages[user_id])).fetch()
            except Exception:
                pass
            del payment_messages[user_id]


async def timer_task_coro(user_id: str, context: MemoryContext):
    try:
        await asyncio.sleep(600)  # 10 минут
        state = await context.get_state()
        if state is not None:
            logging.info(f"Timer expired for user {user_id}, state={state}. Cancelling order.")
            # очищаем состояние, но не отменяем таймер и не шлём "Заказ отменен"
            await reset_logic(user_id, context, send_message=False, cancel_timer=False)
            # теперь отправляем своё уведомление
            await send_message_to_user(
                user_id,
                "⌛ Время оформления заказа истекло. Начните заново:",
                get_main_kb(user_id)
            )
    except asyncio.CancelledError:
        logging.info(f"Timer for user {user_id} was cancelled.")
    except Exception as e:
        logging.error(f"Timer task for user {user_id} failed: {e}", exc_info=True)


def restart_timer(user_id: str, context: MemoryContext):
    logging.info(f"Restarting timer for user {user_id}")
    if user_id in order_timers:
        order_timers[user_id].cancel()
    order_timers[user_id] = asyncio.create_task(timer_task_coro(user_id, context))


# --------------------------
# Обработчики сообщений
# --------------------------
@dp.message_created()
async def handle_messages(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    user_id = get_user_id(event)
    if not user_id:
        return

    state = await context.get_state()
    # Проверка онбординга только если пользователь ещё не в процессе заказа
    if state is None:
        if not await ensure_onboarding(user_id):
            return  # бот сам отправил приветствие или запрос согласия

    # Обработка текста (для комментария)
    text = ""
    if hasattr(event.message, 'body'):
        body = event.message.body
        if isinstance(body, str):
            text = body
        elif isinstance(body, dict):
            text = body.get('text', '')
        elif hasattr(body, 'text'):
            text = body.text

    link = extract_file_link(event)
    photo_url = extract_photo_url(event)

    # ========== РЕЖИМ РАССЫЛКИ (АДМИН) ==========
    if state == Form.admin_broadcast and is_admin(user_id):
        data = broadcast_data.setdefault(user_id, {
            'text': '',
            'photos': [],
            'attachments': [],
            'pending_task': None,
            'media_group_id': None
        })
        pending_task = data.get('pending_task')

        # --- Пришло фото ---
        if photo_url:
            group_id = getattr(event.message, 'media_group_id', None)
            # Обновляем текст, если есть caption
            if text:
                data['text'] = text

            # Добавляем фото в список
            photos = data.setdefault('photos', [])
            photos.append(photo_url)

            if group_id:
                # Альбом: обновляем group_id и запускаем отложенный показ
                data['media_group_id'] = group_id
                if pending_task and not pending_task.done():
                    pending_task.cancel()
                    try:
                        await pending_task
                    except asyncio.CancelledError:
                        pass

                async def delayed_show():
                    await asyncio.sleep(1.0)  # ждём остальные фото альбома
                    cur_data = broadcast_data.get(user_id, {})
                    if cur_data.get('media_group_id') == group_id:
                        await show_broadcast_preview(user_id, context)

                task = asyncio.create_task(delayed_show())
                data['pending_task'] = task
            else:
                # Одиночное фото: просто добавляем в список, ждём «Готово»
                pass

            await send_message_to_user(
                user_id,
                f"📷 Фото {len(photos)}/10 добавлено. Отправьте ещё или нажмите «Готово».",
                get_broadcast_ready_kb()
            )
            return

        # --- Пришёл текст (без фото) ---
        if text:
            if pending_task and not pending_task.done():
                pending_task.cancel()
                try:
                    await pending_task
                except asyncio.CancelledError:
                    pass
            data['text'] = text
            data['pending_task'] = None
            await send_message_to_user(
                user_id,
                "📝 Текст обновлён. Отправьте фото или нажмите «Готово».",
                get_broadcast_ready_kb()
            )
            return

        # Если ничего не подошло
        await send_message_to_user(
            user_id,
            "📝 Пришлите текст или фото для рассылки.",
            get_broadcast_ready_kb()
        )
        return

    if state == Form.file_processing:
        data = await context.get_data()
        if data.get('temp_file'):
            await send_message_to_user(user_id, "❌ Вы уже отправили файл. Дождитесь обработки или отмените заказ.", get_cancel_kb())
            return
        if not link:
            await send_message_to_user(user_id, "❌ Пожалуйста, отправьте файл (документ или фотографию)", get_cancel_kb())
            return

        processing_msg = await send_message_to_user(user_id, "⏳ Файл обрабатывается, подождите пожалуйста...")
        restart_timer(user_id, context)

        try:
            temp_path, file_ext, filename = await download_and_detect_ext(link, user_id)
            if not await is_file_safe(temp_path, file_ext):
                raise ValueError("Файл поврежден или имеет неверный формат")
            if not await scan_file(temp_path):
                raise ValueError("Файл содержит вирусы и был удален")
            pages = await get_page_count(temp_path, file_ext)
            if pages is None or pages < 1:
                raise ValueError("Не удалось определить количество страниц")
            if pages > 500:
                raise ValueError("Слишком много страниц")
            await context.update_data(temp_file=temp_path, pages=pages, file_extension=file_ext[1:], filename=filename)
            await context.set_state(Form.color_selection)
            await send_message_to_user(user_id, f"📄 Файл успешно обработан!\nКоличество страниц: {pages}\nВыберите тип печати:", get_colors_kb())
        except Exception as e:
            logging.error(f"File error: {e}")
            await send_message_to_user(user_id, f"❌ Ошибка обработки: {str(e)}", get_cancel_kb())
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
        finally:
            if processing_msg and hasattr(processing_msg, 'message') and processing_msg.message:
                try:
                    await processing_msg.message.delete()
                except Exception:
                    pass
        return

    if state == Form.comment and text:
        if len(text) > 255:
            await send_message_to_user(user_id, "❌ Комментарий слишком длинный! Максимальная длина — 255 символов", get_skip_kb())
            return
        await process_summary(user_id, context, text)
        restart_timer(user_id, context)
        return

    # Если нет активного состояния – показываем меню
    await send_message_to_user(user_id, "Используйте кнопки главного меню.", get_main_kb(user_id))


async def process_summary(user_id: str, context: MemoryContext, comment_text: str):
    await context.update_data(comment=comment_text)
    data = await context.get_data()
    shop = data["shop"]
    summary = (
        f"🔍 Подтвердите заказ:\n"
        f"• Точка: {shop['name']} по адресу {shop['address']}\n"
        f"• Страниц: {data['pages']}\n"
        f"• Тип печати: {data['color']}\n"
        f"• Стоимость: {data['price']:.2f} руб\n"
        f"• Комментарий: {comment_text if comment_text else 'нет'}\n"
        f"Если все верно — нажмите кнопку '✅ Подтвердить'"
    )
    await context.set_state(Form.confirmation)
    await send_message_to_user(user_id, summary, get_confirm_kb())


# --------------------------
# Обработчик колбэков (ИСПРАВЛЕННЫЙ)
# --------------------------
@dp.message_callback()
async def handle_callbacks(event: MessageCallback, context: MemoryContext):
    try:
        await event.answer()
    except Exception:
        return  # старый или битый колбэк — ничего не делаем

    # 1. Правильное получение идентификаторов через документированный get_ids()
    chat_id, user_id = event.get_ids()
    user_id = str(user_id)
    if chat_id:
        user_chat_mapping[user_id] = chat_id

    payload = event.callback.payload if event.callback else None
    if not payload:
        return

    state = await context.get_state()

    # 2. Обработка кнопки согласия (ДО проверки онбординга)
    if payload == "agree_terms":
        # Подтверждаем получение callback
        # await event.answer()
        # Удаляем сообщение с кнопкой
        if event.message:
            try:
                await event.message.delete()
            except Exception as e:
                logging.error(f"Delete message error: {e}")
        # Фиксируем согласие через API для правильного user_id
        await _mark_agreed(user_id)
        # Отправляем подтверждение и главное меню
        await send_message_to_user(user_id, "✅ Спасибо! Теперь вы можете пользоваться ботом.\n Нажмите «🛒 Новый заказ» для начала.", get_main_kb(user_id))
        return

    # 3. Для всех остальных колбэков проверяем онбординг
    if not await ensure_onboarding(user_id):
        # await event.answer()
        return

    if payload == "broadcast" and is_admin(user_id):
        # Инициализируем данные для этого админа
        broadcast_data[user_id] = {
            'text': '',
            'photos': [],
            'attachments': [],
            'pending_task': None,
            'media_group_id': None
        }
        await context.set_state(Form.admin_broadcast)
        await send_message_to_user(
            user_id,
            "📝 Пришлите текст сообщения (можно с фото).\nДля отмены: кнопка «❌ Отменить».",
            get_cancel_kb()
        )
        return

        # Действия в состоянии сбора рассылки
    if state == Form.admin_broadcast:
        if payload == "broadcast_ready":
            # Пользователь нажал "Готово" – показываем превью
            await show_broadcast_preview(user_id, context)
            return
        elif payload == "reset_order":
            # Отмена рассылки
            broadcast_data.pop(user_id, None)
            await context.clear()
            await send_message_to_user(user_id, "❌ Рассылка отменена.", get_main_kb(user_id))
            return

        # Действия в состоянии подтверждения рассылки
    if state == Form.confirm_broadcast:
        if payload == "send_broadcast":
            await send_broadcast(user_id, context)
            return
        elif payload == "reset_order":
            broadcast_data.pop(user_id, None)
            await context.clear()
            await send_message_to_user(user_id, "❌ Рассылка отменена.", get_main_kb(user_id))
            return

    # Стандартные действия
    if payload == "cancel_and_start":
        await reset_logic(user_id, context)
        await handle_new_order(user_id, context, event)
        return
    if payload == "continue_current":
        await send_message_to_user(user_id, "Пожалуйста, завершите оплату текущего заказа. Если возникли проблемы, используйте кнопку «Отменить».")
        return
    if payload == "reset_order":
        if user_id in processing_cancel:
            # await event.answer()
            return
        processing_cancel.add(user_id)
        try:
            broadcast_data.pop(user_id, None)
            active = user_id in active_orders
            await reset_logic(user_id, context, send_message=not active)
        finally:
            processing_cancel.discard(user_id)
        # await event.answer()
        return
    if payload == "help":
        text = (
            "Если у вас возникли вопросы, проблемы с заказом или вам просто нужна консультация — обратитесь в нашу службу поддержки.\n\n"
            "📞 Контакты:\n"
            "• Email: send-to-print-and-pick-up@yandex.ru\n"
            "❗ Время ответа от поддержки может занимать до 72 часов\n"
            "Мы обязательно вам поможем! 😊\n\n"
            "📚 <a href='https://disk.yandex.ru/d/Q-1xYZuSQFZNYA'>Документация сервиса Send to print and pick up!</a>"

        )
        await send_message_to_user(
            user_id, text, get_main_kb(user_id), format=ParseMode.HTML
        )
        # await event.answer()
        return
    if payload == "new_order" and state is None:
        await handle_new_order(user_id, context, event)
        return

    # Логика выбора магазина
    if state == Form.shop_selection and payload.startswith("shop_"):
        sid = int(payload.split("_")[1])
        data = await context.get_data()
        basic_shop = next((s for s in data.get("shops_list", []) if s['ID_shop'] == sid), None)
        if basic_shop:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{API_URL}/shops/{basic_shop['name']}", headers={"x-api-key": INTERNAL_API_KEY}) as r:
                        if r.status == 200:
                            detailed_shop = await r.json()
                            await context.update_data(shop=detailed_shop)
                            await context.set_state(Form.file_processing)
                            resp = (f"🏪 Выбрана точка: {detailed_shop['name']}\n"
                                    f"⌚ Время работы: {detailed_shop['w_hours']}\n"
                                    f"📍 Адрес: {detailed_shop['address']}\n"
                                    f"💰 Цены:\n"
                                    f"• Черно-белая: {detailed_shop['price_bw']:.2f} руб/стр\n"
                                    f"• Цветная: {detailed_shop['price_cl']:.2f} руб/стр\n\n"
                                    f"📎 Отправьте один PDF, DOC, DOCX, PNG, JPEG, JPG файл или одну фотографию размером не более 20 МБ для расчета стоимости")
                            await send_message_to_user(user_id, resp, get_cancel_kb())
                            restart_timer(user_id, context)
                        else:
                            await send_message_to_user(user_id, "❌ Точка не найдена.", get_main_kb(user_id))
                            await context.clear()
            except Exception as e:
                logging.error(f"Error fetching shop: {e}")
                await send_message_to_user(user_id, "❌ Ошибка при получении информации.", get_main_kb(user_id))
        else:
            await send_message_to_user(user_id, "❌ Точка не найдена.", get_main_kb(user_id))
            await context.clear()
        return

    # Выбор цвета
    if state == Form.color_selection and payload.startswith("color_"):
        color = "черно-белая" if payload == "color_bw" else "цветная"
        data = await context.get_data()
        shop = data['shop']
        price = shop['price_bw'] if color == "черно-белая" else shop['price_cl']
        total_price = round(price * data['pages'], 2)
        await context.update_data(color=color, price=total_price)
        await context.set_state(Form.comment)
        await send_message_to_user(user_id, "📝 Введите комментарий к заказу или нажмите кнопку ниже:", get_skip_kb())
        restart_timer(user_id, context)
        return

    # Пропуск комментария
    if state == Form.comment and payload == "skip_comment":
        await process_summary(user_id, context, "")
        restart_timer(user_id, context)
        return

    # Подтверждение заказа
    if state == Form.confirmation and payload == "confirm_order":
        # Защита от повторного нажатия
        if user_id in processing_payment:
            # await event.answer()  # просто подтверждаем, ничего не делаем
            return

        processing_payment.add(user_id)

        if user_id in order_timers:
            order_timers[user_id].cancel()
            del order_timers[user_id]

        data = await context.get_data()
        check_code = random.randint(1000, 9999)

        # Временное сообщение
        processing_msg = await send_message_to_user(user_id, "⏳ Ссылка на оплату формируется, подождите пожалуйста...")
        proc_mid = None
        if processing_msg and hasattr(processing_msg, 'message') and processing_msg.message:
            proc_mid = getattr(processing_msg.message.body, 'mid', None)

        order_id = None
        try:
            async with aiohttp.ClientSession() as session:
                form_data = aiohttp.FormData()
                form_data.add_field("ID_shop", str(data["shop"]["ID_shop"]))
                form_data.add_field("price", str(data["price"]))
                form_data.add_field("pages", str(data["pages"]))
                form_data.add_field("color", data["color"])
                form_data.add_field("user_id", str(user_id))
                form_data.add_field("note", data.get("comment", ""))
                form_data.add_field("file_extension", data["file_extension"])
                form_data.add_field("platform", "max")
                form_data.add_field("con_code", str(check_code))
                with open(data["temp_file"], "rb") as f:
                    form_data.add_field("file", f.read(), filename=data["filename"])
                headers = {"x-api-key": INTERNAL_API_KEY}
                async with session.post(f"{API_URL}/orders", data=form_data, headers=headers) as resp:
                    if resp.status != 201:
                        raise Exception(f"Ошибка создания заказа: {await resp.text()}")
                    order_res = await resp.json()
                    order_id = order_res["order_id"]
                    logging.info(f"Order {order_id} created successfully")
                async with session.post(f"{API_URL}/payments/create", json={"order_id": order_id},
                                        headers=headers) as payment_resp:
                    if payment_resp.status != 200:
                        raise Exception(f"Ошибка платежа: {await payment_resp.text()}")
                    payment_info = await payment_resp.json()
                    confirmation_url = payment_info.get("confirmation_url")

            await context.set_state(None)
            msg_text = f"💳 Для завершения заказа перейдите по ссылке:\n{confirmation_url}\n❗ Ссылка будет действительна в течение 10 минут"
            sent_msg = await send_message_to_user(user_id, msg_text, get_confirm_kb(confirmation_url))

            if sent_msg and hasattr(sent_msg, 'message') and sent_msg.message:
                payment_messages[user_id] = sent_msg.message.body.mid
            active_orders[user_id] = order_id
            logging.info(f"Order {order_id} saved to active_orders for user {user_id}")

            if os.path.exists(data["temp_file"]):
                os.remove(data["temp_file"])

        except Exception as e:
            logging.error(f"Payment creation error: {traceback.format_exc()}")
            if order_id:
                await cancel_order_via_api(order_id)
            await context.clear()
            await send_message_to_user(user_id, "❌ Произошла ошибка при создании заказа/платежа. Попробуйте позже",
                                       get_main_kb(user_id))
        finally:
            processing_payment.discard(user_id)
            # Удаляем временное сообщение
            if proc_mid:
                chat_id = user_chat_mapping.get(user_id)
                if chat_id:
                    try:
                        await DeleteMessage(bot, str(proc_mid)).fetch()
                    except Exception as e:
                        logging.error(f"Delete processing msg error: {e}")
        return


async def handle_new_order(user_id: str, context: MemoryContext, event):
    state = await context.get_state()
    if state is not None:
        return
    if user_id in active_orders and await has_active_order(user_id):
        await send_message_to_user(user_id, "У вас есть незавершённый заказ.", get_active_order_kb())
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_URL}/shops", headers={"x-api-key": INTERNAL_API_KEY}) as r:
                if r.status == 200:
                    shops = await r.json()
                    await context.update_data(shops_list=shops)
                    await context.set_state(Form.shop_selection)
                    await send_message_to_user(user_id, "🏪 Выберите точку печати из списка:", get_shops_kb(shops))
                    restart_timer(user_id, context)
                else:
                    await send_message_to_user(user_id, "❌ Ошибка загрузки магазинов", get_main_kb(user_id))
    except Exception as e:
        logging.error(f"Error fetching shops: {e}")
        await send_message_to_user(user_id, "❌ Ошибка соединения.", get_main_kb(user_id))


# --------------------------
# Функции рассылки
# --------------------------
async def show_broadcast_preview(user_id: str, context: MemoryContext):
    """Скачивает фото, формирует вложения и показывает красивое превью."""
    data = broadcast_data.get(user_id, {})
    text = data.get('text', '')
    photos = data.get('photos', [])

    # Отменяем отложенную задачу альбома, если есть
    pending_task = data.get('pending_task')
    if pending_task and not pending_task.done():
        pending_task.cancel()
        try:
            await pending_task
        except asyncio.CancelledError:
            pass

    data['pending_task'] = None
    data['media_group_id'] = None
    await context.set_state(Form.confirm_broadcast)

    # Скачиваем фото и создаём вложения
    attachments = []
    for url in photos:
        try:
            photo_data = await download_file_from_url(url)
            media = InputMediaBuffer(photo_data, filename="photo.jpg")
            attachments.append(media)
        except Exception as e:
            logging.error(f"Preview download error: {e}")
            await send_message_to_user(user_id, "❌ Ошибка загрузки фото.", get_main_kb(user_id))
            return

    # Сохраняем attachments для использования при отправке
    data['attachments'] = attachments
    broadcast_data[user_id] = data

    # Формируем красивое текстовое превью
    preview_lines = ["👇 Предпросмотр рассылки:", ""]
    if text:
        preview_lines.append(text)
        preview_lines.append("")  # пустая строка для отступа
    if photos:
        preview_lines.append(f"📷 Фото: {len(photos)} шт.")
        preview_lines.append("")
    preview_lines.append("Отправить это сообщение всем пользователям?")
    preview_text = "\n".join(preview_lines)

    # Отправляем превью (текст + фото) одним сообщением
    try:
        await SendMessage(
            bot,
            chat_id=user_chat_mapping[user_id],
            text=preview_text,
            attachments=attachments
        ).fetch()
    except Exception as e:
        logging.error(f"Preview send error: {e}")
        await send_message_to_user(user_id, "❌ Ошибка отправки предпросмотра.", get_main_kb(user_id))
        return

    # Отправляем кнопки подтверждения отдельным сообщением
    await send_message_to_user(
        user_id,
        "Подтвердите отправку:",
        get_broadcast_confirm_kb()
    )


async def send_broadcast(user_id: str, context: MemoryContext):
    """Рассылает сообщение всем согласившимся пользователям."""
    data = broadcast_data.pop(user_id, {})
    text = data.get('text', '')
    attachments = data.get('attachments', [])

    await context.clear()

    # Получаем список согласившихся
    headers = {"X-API-Key": INTERNAL_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/users/agreed-ids?platform=max", headers=headers) as resp:
            if resp.status != 200:
                await send_message_to_user(user_id, "❌ Ошибка получения списка пользователей.", get_main_kb(user_id))
                return
            user_ids = await resp.json()

    total = len(user_ids)
    if total == 0:
        await send_message_to_user(user_id, "❌ Нет пользователей для рассылки.", get_main_kb(user_id))
        return

    await send_message_to_user(user_id, f"⏳ Начинаю рассылку для {total} получателей...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            if attachments:
                await SendMessage(
                    bot,
                    chat_id=user_chat_mapping.get(uid, int(uid)),
                    text=text,
                    attachments=attachments
                ).fetch()
            else:
                await send_message_to_user(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Broadcast to {uid} failed: {e}")
            failed += 1

    await send_message_to_user(
        user_id,
        f"📊 Итог рассылки:\n✅ Успешно: {sent}\n❌ Ошибок: {failed}",
        get_main_kb(user_id)
    )


# --------------------------
# HTTP сервер для уведомлений (без изменений)
# --------------------------
async def handle_notify(request: web.Request):
    if request.headers.get("X-Internal-Key") != INTERNAL_API_KEY:
        return web.Response(status=403)
    try:
        data = await request.json()
        user_id = str(data['user_id'])
        order_id = data['order_id']
        status = data['status']
        address = data.get('address', '')
        con_code = data.get('con_code', '')
        logging.info(f"Notify received: user_id={user_id}, order_id={order_id}, status={status}")
        if status in ('paid', 'expired', 'canceled', 'ready', 'completed'):
            if user_id in active_orders and active_orders[user_id] == order_id:
                del active_orders[user_id]
                await delete_message_for_user(user_id)
        text = ""
        if status == 'paid':
            text = f"✅ Оплата прошла успешно! Заказ №{order_id} принят в работу.\nПо готовности вам придет уведомление"
        elif status == 'ready':
            text = f"🖨️ Заказ №{order_id} готов!\n• Адрес получения: {address}\n• Проверочный код: {con_code}\nПожалуйста, назовите этот код сотруднику, чтобы забрать заказ"
        elif status == 'completed':
            text = f"✅ Заказ №{order_id} выдан! Спасибо, что воспользовались нашим сервисом! Ждем вас снова!"
        elif status == 'expired':
            text = f"⌛ Время оплаты заказа №{order_id} истекло. Заказ отменён.\nВы можете создать новый заказ"
        elif status == 'canceled':
            text = f"❌ Платёж по заказу №{order_id} был отклонён или отменён.\nВы можете попробовать оплатить снова, создав новый заказ"
        if text:
            await send_message_to_user(user_id, text, get_main_kb(user_id))
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Notify Error: {traceback.format_exc()}")
        return web.Response(status=500)


async def start_http_server():
    app = web.Application()
    app.router.add_post('/notify', handle_notify)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8004)
    await site.start()
    logging.info("MAX HTTP notification server started on port 8004")


# --------------------------
# Обработчик первого запуска бота
# --------------------------
@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = str(event.user.user_id)
    chat_id = event.chat_id
    if chat_id:
        user_chat_mapping[user_id] = chat_id

    # Получаем имя пользователя (если есть)
    first = getattr(event.user, 'first_name', None)
    name = str(first) if first else "друг"

    # Проверяем, приветствовали ли мы его ранее
    status = await _get_onboarding_status(user_id)
    if not status.get("welcomed", False):
        await _mark_welcomed(user_id)           # отмечаем welcomed
        await _send_welcome_and_agreement(user_id, name=name)
        return

    # Если уже приветствован, но не согласился – напомним
    if not status.get("agreed", False):
        await _request_agreement(user_id)


async def main():
    if not MAX_BOT_TOKEN:
        logging.error("MAX_BOT_TOKEN is missing!")
        return
    logging.info("Starting MAX Bot and HTTP notification server...")
    await asyncio.gather(
        start_http_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())