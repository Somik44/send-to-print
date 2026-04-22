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
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from aiohttp import web
from maxapi import Bot, Dispatcher
from maxapi.context import MemoryContext, StatesGroup, State
from maxapi.types import MessageCreated, Command, MessageCallback
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import LinkButton, CallbackButton
from maxapi.methods.send_message import SendMessage
from maxapi.methods.delete_message import DeleteMessage
from PIL import Image
import io
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


# --------------------------
# Вспомогательные функции
# --------------------------
def get_user_id(event) -> str:
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
    if hasattr(event, 'message') and hasattr(event.message, 'recipient') and hasattr(event.message.recipient, 'chat_id'):
        return event.message.recipient.chat_id
    if hasattr(event, 'chat_id'):
        return event.chat_id
    return None


def is_admin(user_id: str) -> bool:
    return int(user_id) in ADMIN_IDS_MAX


def update_chat_mapping(event):
    user_id = get_user_id(event)
    chat_id = get_chat_id(event)
    if user_id and chat_id:
        user_chat_mapping[user_id] = chat_id
        logging.debug(f"Mapping updated: user {user_id} -> chat {chat_id}")
    else:
        logging.debug(f"Could not extract mapping from event: {event}")


async def send_message_to_user(user_id: str, text: str, attachments=None):
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
            attachments=attachments
        ).fetch()
        return msg
    except Exception as e:
        logging.error(f"Failed to send message to user {user_id}: {e}")
        return None


def extract_message_id(sent_msg) -> str | None:
    """Извлекает mid из объекта SendedMessage."""
    try:
        if sent_msg and hasattr(sent_msg, 'message') and sent_msg.message:
            if hasattr(sent_msg.message, 'body') and sent_msg.message.body:
                return getattr(sent_msg.message.body, 'mid', None)
    except Exception as e:
        logging.error(f"Error extracting message_id: {e}")
    return None


async def delete_message(chat_id: int, message_id):
    """Удаляет сообщение по его ID с логированием результата."""
    try:
        message_id_str = str(message_id)
        logging.info(f"Attempting to delete message {message_id_str} in chat {chat_id}")
        result = await DeleteMessage(bot, message_id_str).fetch()
        logging.info(f"Message {message_id_str} deleted successfully: {result}")
    except Exception as e:
        logging.error(f"Failed to delete message {message_id}: {type(e).__name__}: {e}")


async def delete_message_for_user(user_id: str):
    """Удаляет платёжное сообщение пользователя."""
    if user_id in payment_messages:
        chat_id = user_chat_mapping.get(user_id)
        if chat_id:
            await delete_message(chat_id, payment_messages[user_id])
            del payment_messages[user_id]
        else:
            logging.warning(f"Cannot delete payment message for user {user_id}: chat_id not found")


async def setup_bot_commands():
    token = os.getenv("MAX_BOT_TOKEN")
    if not token:
        logging.error("MAX_BOT_TOKEN not set, cannot setup commands")
        return

    commands = [
        {"name": "start", "description": "Запустить бота / Главное меню"},
        {"name": "new_order", "description": "Создать новый заказ"},
        {"name": "reset", "description": "Сбросить текущий заказ"},
        {"name": "help", "description": "Помощь и инструкции"}
    ]

    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.patch(
            "https://platform-api.max.ru/me",
            json={"commands": commands},
            headers=headers
        ) as resp:
            if resp.status == 200:
                logging.info("Bot commands set up successfully")
            else:
                text = await resp.text()
                logging.error(f"Failed to set commands: {resp.status} - {text}")


# --------------------------
# Состояния (FSM)
# --------------------------
class Form(StatesGroup):
    shop_selection = State()
    file_processing = State()
    color_selection = State()
    comment = State()
    confirmation = State()


# --------------------------
# Клавиатуры (Inline)
# --------------------------
def get_main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="🛒 Новый заказ", payload="new_order"))
    builder.row(CallbackButton(text="ℹ️ Помощь", payload="help"))
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
        CallbackButton(text="⚫ Черно-белая", payload="color_bw"),
        CallbackButton(text="🌈 Цветная", payload="color_cl")
    )
    builder.row(CallbackButton(text="❌ Отменить", payload="reset_order"))
    return [builder.as_markup()]


def get_skip_kb():
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="⏭ Без комментария", payload="skip_comment"))
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
    builder.row(CallbackButton(text="❌ Отменить и начать заново", payload="cancel_and_start"))
    builder.row(CallbackButton(text="✅ Продолжить текущий", payload="continue_current"))
    return [builder.as_markup()]


# --------------------------
# Работа с файлами
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

    # Если это WebP, конвертируем в JPEG
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
            'image/webp': '.webp'  # уже не нужно
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


async def reset_logic(event, context: MemoryContext):
    user_id = get_user_id(event)
    if not user_id:
        return

    order_id = active_orders.pop(user_id, None)
    if order_id:
        await cancel_order_via_api(order_id)

    await delete_message_for_user(user_id)

    if user_id in order_timers:
        order_timers[user_id].cancel()
        del order_timers[user_id]

    data = await context.get_data()
    temp_file = data.get("temp_file")
    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except Exception:
            pass

    await context.clear()
    await send_message_to_user(user_id, "❌ Заказ отменен", get_main_kb())


async def timer_task_coro(user_id: str, context: MemoryContext, event):
    try:
        await asyncio.sleep(600)
        state = await context.get_state()
        if state is not None:
            await reset_logic(event, context)
            await send_message_to_user(
                user_id,
                "⌛ Время оформления заказа истекло. Начните заново:",
                get_main_kb()
            )
    except asyncio.CancelledError:
        pass


def restart_timer(user_id: str, context: MemoryContext, event):
    if user_id in order_timers:
        order_timers[user_id].cancel()
    order_timers[user_id] = asyncio.create_task(timer_task_coro(user_id, context, event))


async def get_user_full_name(event: MessageCreated) -> str:
    try:
        if hasattr(event.message.sender, 'first_name'):
            first = event.message.sender.first_name or ""
            last = event.message.sender.last_name or ""
            return f"{first} {last}".strip()
    except Exception:
        pass
    return "друг"


# --------------------------
# Обработчики команд
# --------------------------
@dp.message_created(Command('start'))
async def cmd_start(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    user_id = get_user_id(event)
    name = await get_user_full_name(event)

    if user_id in active_orders and await has_active_order(user_id):
        await send_message_to_user(
            user_id,
            "У вас есть незавершённый заказ (ожидает оплаты). Что хотите сделать?",
            get_active_order_kb()
        )
        return

    await reset_logic(event, context)
    welcome_text = f"Привет, {name}! Рады приветствовать тебя на нашем сервисе по печати документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order"
    await send_message_to_user(user_id, welcome_text, get_main_kb())


@dp.message_created(Command('new_order'))
async def cmd_new_order(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    user_id = get_user_id(event)

    if user_id in active_orders and await has_active_order(user_id):
        await send_message_to_user(
            user_id,
            "У вас есть незавершённый заказ (ожидает оплаты). Что хотите сделать?",
            get_active_order_kb()
        )
        return

    await handle_new_order(event, context)


@dp.message_created(Command('reset'))
async def cmd_reset(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    await reset_logic(event, context)


@dp.message_created(Command('help'))
async def cmd_help(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    user_id = get_user_id(event)
    await send_message_to_user(
        user_id,
        "📖 Помощь:\n"
        "1. Нажмите 'Новый заказ' для начала.\n"
        "2. Выберите точку печати.\n"
        "3. Отправьте документ или фото.\n"
        "4. Выберите тип печати (черно-белая или цветная).\n"
        "5. При желании добавьте комментарий.\n"
        "6. Подтвердите заказ и оплатите.\n"
        "7. После оплаты заказ поступит в работу, вы получите уведомление о готовности.\n\n"
        "Если возникнут вопросы, обращайтесь к администратору",
        get_main_kb()
    )


async def handle_new_order(event, context: MemoryContext):
    update_chat_mapping(event)
    user_id = get_user_id(event)
    state = await context.get_state()

    if state is not None:
        return

    if user_id in active_orders and await has_active_order(user_id):
        await send_message_to_user(
            user_id,
            "У вас есть незавершённый заказ (ожидает оплаты). Что хотите сделать?",
            get_active_order_kb()
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_URL}/shops", headers={"x-api-key": INTERNAL_API_KEY}) as r:
                if r.status == 200:
                    shops = await r.json()
                    await context.update_data(shops_list=shops)
                    await context.set_state(Form.shop_selection)
                    await send_message_to_user(
                        user_id,
                        "🏪 Выберите точку печати из списка:",
                        get_shops_kb(shops)
                    )
                    restart_timer(user_id, context, event)
                else:
                    await send_message_to_user(
                        user_id,
                        "❌ Ошибка загрузки магазинов",
                        get_main_kb()
                    )
    except Exception as e:
        logging.error(f"Error fetching shops list: {e}")
        await send_message_to_user(
            user_id,
            "❌ Ошибка соединения. Попробуйте позже.",
            get_main_kb()
        )


# --------------------------
# Callback-обработчик
# --------------------------
@dp.message_callback()
async def handle_callbacks(event: MessageCallback, context: MemoryContext):
    update_chat_mapping(event)

    payload = None
    if hasattr(event, 'payload'):
        payload = event.payload
    elif hasattr(event, 'callback') and hasattr(event.callback, 'payload'):
        payload = event.callback.payload
    elif hasattr(event, 'data'):
        payload = event.data

    if not payload:
        return

    user_id = get_user_id(event)
    if not user_id:
        logging.error("Cannot determine user_id from callback")
        return

    state = await context.get_state()

    if payload == "cancel_and_start":
        await reset_logic(event, context)
        await handle_new_order(event, context)
        return
    if payload == "continue_current":
        await send_message_to_user(
            user_id,
            "Пожалуйста, завершите оплату текущего заказа. Если возникли проблемы, используйте /reset для отмены"
        )
        return

    if payload == "reset_order":
        await reset_logic(event, context)
        return

    if payload == "help":
        await cmd_help(event, context)
        return

    if payload == "new_order" and state is None:
        await handle_new_order(event, context)
        return

    if state == Form.shop_selection and payload.startswith("shop_"):
        sid = int(payload.split("_")[1])
        data = await context.get_data()
        basic_shop = next((s for s in data.get("shops_list", []) if s['ID_shop'] == sid), None)

        if basic_shop:
            shop_name = basic_shop['name']
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{API_URL}/shops/{shop_name}",
                                           headers={"x-api-key": INTERNAL_API_KEY}) as r:
                        if r.status == 200:
                            detailed_shop = await r.json()
                            await context.update_data(shop=detailed_shop)
                            await context.set_state(Form.file_processing)
                            resp = (
                                f"🏪 Выбрана точка: {detailed_shop['name']}\n"
                                f"⌚ Время работы: {detailed_shop['w_hours']}\n"
                                f"📍 Адрес: {detailed_shop['address']}\n"
                                f"💰 Цены:\n"
                                f"• Черно-белая: {detailed_shop['price_bw']:.2f} руб/стр\n"
                                f"• Цветная: {detailed_shop['price_cl']:.2f} руб/стр\n\n"
                                f"📎 Отправьте один PDF, DOC, DOCX, PNG, JPEG, JPG, WEBP файл или одну фотографию размером не более 20 МБ для расчета стоимости\n"
                                f"Используйте /reset для отмены заказа"
                            )
                            await send_message_to_user(user_id, resp, get_cancel_kb())
                            restart_timer(user_id, context, event)
                        else:
                            await send_message_to_user(
                                user_id,
                                "❌ Точка не найдена. /new_order",
                                get_main_kb()
                            )
                            await context.clear()
            except Exception as e:
                logging.error(f"Error fetching detailed shop info: {e}")
                await send_message_to_user(
                    user_id,
                    "❌ Ошибка при получении информации о точке. Попробуйте позже",
                    get_main_kb()
                )
        else:
            await send_message_to_user(
                user_id,
                "❌ Точка не найдена. /new_order",
                get_main_kb()
            )
            await context.clear()
        return

    if state == Form.color_selection and payload.startswith("color_"):
        color = "черно-белая" if payload == "color_bw" else "цветная"
        data = await context.get_data()
        shop = data['shop']
        price = shop['price_bw'] if color == "черно-белая" else shop['price_cl']
        total_price = round(price * data['pages'], 2)

        await context.update_data(color=color, price=total_price)
        await context.set_state(Form.comment)
        await send_message_to_user(
            user_id,
            "📝 Введите комментарий к заказу или нажмите кнопку ниже:",
            get_skip_kb()
        )
        restart_timer(user_id, context, event)
        return

    if state == Form.comment and payload == "skip_comment":
        await process_summary(event, context, "")
        restart_timer(user_id, context, event)
        return

    if state == Form.confirmation and payload == "confirm_order":
        if user_id in order_timers:
            order_timers[user_id].cancel()
            del order_timers[user_id]

        data = await context.get_data()
        check_code = random.randint(1000, 9999)

        processing_msg = await send_message_to_user(user_id, "⏳ Ссылка на оплату формируется, подождите пожалуйста...")
        proc_mid = extract_message_id(processing_msg)
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

            payment_mid = extract_message_id(sent_msg)
            if payment_mid:
                payment_messages[user_id] = payment_mid
                logging.info(f"Saved payment message {payment_mid} for user {user_id}")

            active_orders[user_id] = order_id
            logging.info(f"Order {order_id} saved to active_orders for user {user_id}")

            if os.path.exists(data["temp_file"]):
                os.remove(data["temp_file"])

            # Удаляем сообщение "Ссылка формируется..."
            if proc_mid:
                chat_id = user_chat_mapping.get(user_id)
                if chat_id:
                    # await asyncio.sleep(0.5)
                    await delete_message(chat_id, proc_mid)

        except Exception as e:
            logging.error(f"Payment creation error: {traceback.format_exc()}")
            if order_id:
                await cancel_order_via_api(order_id)
            await context.clear()
            await send_message_to_user(
                user_id,
                "❌ Произошла ошибка при создании заказа/платежа. Попробуйте позже",
                get_main_kb()
            )
        return


# --------------------------
# Обработчик сообщений (файлы/текст)
# --------------------------
@dp.message_created()
async def handle_messages(event: MessageCreated, context: MemoryContext):
    update_chat_mapping(event)
    state = await context.get_state()
    user_id = get_user_id(event)

    text = ""
    if hasattr(event.message, 'body'):
        body = event.message.body
        if isinstance(body, str):
            text = body
        elif isinstance(body, dict):
            text = body.get('text', '')
        elif hasattr(body, 'text'):
            text = body.text

    if text.startswith('/'):
        return

    link = extract_file_link(event)

    if state == Form.file_processing:
        data = await context.get_data()
        if data.get('temp_file'):
            await send_message_to_user(
                user_id,
                "❌ Вы уже отправили файл. Дождитесь обработки или отмените текущий заказ командой /reset",
                get_cancel_kb()
            )
            return

        if not link:
            await send_message_to_user(
                user_id,
                "❌ Пожалуйста, отправьте файл (документ или фотографию)",
                get_cancel_kb()
            )
            return

        processing_msg = await send_message_to_user(user_id, "⏳ Файл обрабатывается, подождите пожалуйста...")
        proc_mid = extract_message_id(processing_msg)
        restart_timer(user_id, context, event)

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
            await send_message_to_user(
                user_id,
                f"📄 Файл успешно обработан!\nКоличество страниц: {pages}\nВыберите тип печати:",
                get_colors_kb()
            )

        except Exception as e:
            logging.error(f"File error: {e}")
            await send_message_to_user(
                user_id,
                f"❌ Ошибка обработки: {str(e)}",
                get_cancel_kb()
            )
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.remove(temp_path)
        finally:
            if proc_mid:
                chat_id = user_chat_mapping.get(user_id)
                if chat_id:
                    # await asyncio.sleep(0.5)
                    await delete_message(chat_id, proc_mid)
        return

    if state == Form.comment and text:
        if len(text) > 255:
            await send_message_to_user(
                user_id,
                "❌ Комментарий слишком длинный! Максимальная длина — 255 символов",
                get_skip_kb()
            )
            return
        await process_summary(event, context, text)
        restart_timer(user_id, context, event)
        return


async def process_summary(event, context: MemoryContext, comment_text: str):
    user_id = get_user_id(event)
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
# HTTP сервер для уведомлений от API (как в VK)
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
                logging.info(f"Removed order {order_id} from active_orders for user {user_id}")
                logging.info(f"Attempting to delete payment message for user {user_id}")
                await delete_message_for_user(user_id)

        text = ""
        if status == 'paid':
            text = f"✅ Оплата прошла успешно! Заказ №{order_id} принят в работу.\nПо готовности вам придет уведомление"
        elif status == 'ready':
            text = f"🖨️ Заказ №{order_id} готов!\n• Адрес получения: {address}\n• Проверочный код: {con_code}\nПожалуйста, назовите этот код сотруднику, чтобы забрать заказ"
        elif status == 'completed':
            text = f"✅ Заказ №{order_id} выдан! Спасибо, что воспользовались нашим сервисом! Ждем вас снова!"
        elif status == 'expired':
            text = f"⌛ Время оплаты заказа №{order_id} истекло. Заказ отменён.\nВы можете создать новый заказ: /new_order"
        elif status == 'canceled':
            text = f"❌ Платёж по заказу №{order_id} был отклонён или отменён.\nВы можете попробовать оплатить снова, создав новый заказ: /new_order"

        if text:
            await send_message_to_user(user_id, text, get_main_kb())

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
# Запуск бота
# --------------------------
async def main():
    if not MAX_BOT_TOKEN:
        logging.error("MAX_BOT_TOKEN is missing!")
        return

    logging.info("Starting MAX Bot and HTTP notification server...")
    await setup_bot_commands()
    await asyncio.gather(
        start_http_server(),
        dp.start_polling(bot)
    )


if __name__ == "__main__":
    asyncio.run(main())