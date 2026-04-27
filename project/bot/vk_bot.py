import os
import logging
import asyncio
import aiohttp
import random
import traceback
import uuid
import aiofiles
import json
from logging.handlers import RotatingFileHandler
from vkbottle.bot import Bot, Message
from vkbottle import (
    Keyboard, KeyboardButtonColor, Text, BaseStateGroup,
    GroupEventType, GroupTypes
)
from aiohttp import web
from utils import (
    download_file_from_url,
    get_page_count,
    is_file_safe,
    scan_file,
    MAX_FILE_SIZE,
    UPLOAD_FOLDER
)

# --------------------------
# Настройка логирования
# --------------------------
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = 'vk_bot.log'
handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
handler.setFormatter(log_formatter)
handler.setLevel(logging.DEBUG)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)

# --------------------------
# Загрузка переменных окружения
# --------------------------
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(env_path)

VK_TOKEN = os.getenv("VK_BOT_TOKEN")
API_URL = os.getenv("API_URL")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
ADMIN_IDS_VK = os.getenv("ADMIN_IDS_VK")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_VK.split(",") if id.strip()]


# --------------------------
# Определение состояний
# --------------------------
class States(BaseStateGroup):
    SHOP_SELECTION = "shop_selection"
    FILE_PROCESSING = "file_processing"
    COLOR_SELECTION = "color_selection"
    COMMENT = "comment"
    CONFIRMATION = "confirmation"


class BroadcastStates(BaseStateGroup):
    WAITING_MESSAGE = "waiting_message"
    CONFIRM = "confirm"


# --------------------------
# Класс VK бота
# --------------------------
class VKPrintBot:
    def __init__(self, token: str):
        self.bot = Bot(token=token)
        self.user_data = {}          # user_id -> временные данные
        self.active_orders = {}      # user_id -> order_id
        self.payment_messages = {}   # user_id -> message_id (ID сообщения с платёжной ссылкой)
        self.order_timers = {}       # user_id -> asyncio.Task (10 мин на оформление)
        self.broadcast_data = {}
        self.setup_handlers()

    # --------------------------
    # Вспомогательные функции
    # --------------------------
    async def get_user_full_name(self, vk_id: int) -> str:
        try:
            users = await self.bot.api.users.get(user_ids=vk_id)
            if users:
                user = users[0]
                return f"{user.first_name} {user.last_name}".strip()
            return "Друг"
        except Exception as e:
            logging.error(f"Error getting VK user name {vk_id}: {e}")
            return "Друг"

    def is_admin(self, vk_id: int) -> bool:
        return vk_id in ADMIN_IDS

    async def _get_onboarding_status(self, user_id: int) -> dict:
        """Возвращает {'welcomed': bool, 'agreed': bool} или None при ошибке."""
        headers = {"x-api-key": INTERNAL_API_KEY}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                        f"{API_URL}/users/{user_id}/onboarding-status",
                        params={"platform": "vk"},
                        headers=headers
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception as e:
                logging.error(f"onboarding-status request failed: {e}")
        return None

    async def _mark_welcomed(self, user_id: int):
        headers = {"x-api-key": INTERNAL_API_KEY}
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(
                    f"{API_URL}/users/{user_id}/onboarding-welcome",
                    params={"platform": "vk"},
                    headers=headers
                )
            except Exception as e:
                logging.error(f"mark_welcomed error: {e}")

    async def _mark_agreed(self, user_id: int):
        headers = {"x-api-key": INTERNAL_API_KEY}
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(
                    f"{API_URL}/users/{user_id}/onboarding-agree",
                    params={"platform": "vk"},
                    headers=headers
                )
            except Exception as e:
                logging.error(f"mark_agreed error: {e}")

    async def ensure_onboarding(self, user_id: int) -> bool:
        """Проверяет onboarding; если False – отправляет нужное сообщение и возвращает False."""
        status = await self._get_onboarding_status(user_id)
        if status is None:  # ошибка API – на всякий случай блокируем
            return False

        welcomed = status.get("welcomed", False)
        agreed = status.get("agreed", False)

        if not welcomed:
            # Отмечаем, что приветствие отправлено (однократно)
            await self._mark_welcomed(user_id)
            # Отправляем приветствие + кнопку «Согласен»
            await self._send_welcome_and_agreement(user_id)
            return False

        if not agreed:
            # Приветствие уже было, но согласия нет – напоминаем
            await self._request_agreement(user_id)
            return False

        return True

    async def _send_welcome_and_agreement(self, user_id: int):
        try:
            full_name = await self.get_user_full_name(user_id)
        except Exception:
            full_name = "друг"

        text = (
            f"Привет, {full_name}! Рады приветствовать тебя на нашем сервисе по печати "
            f"документов в любое удобное время! Прежде чем начать, пожалуйста, ознакомьтесь с правилами и нажмите кнопку ниже.\n\n"
            "📚 Документация сервиса Send to print and pick up: https://disk.yandex.ru/d/Q-1xYZuSQFZNYA "
        )
        keyboard = Keyboard(inline=False)
        keyboard.add(Text("✅ Согласен"), color=KeyboardButtonColor.POSITIVE)
        await self.bot.api.messages.send(
            user_id=user_id,
            message=text,
            keyboard=keyboard.get_json(),
            random_id=random.randint(0, 2 ** 31 - 1)
        )

    async def _request_agreement(self, user_id: int):
        keyboard = Keyboard(inline=False)
        keyboard.add(Text("✅ Согласен"), color=KeyboardButtonColor.POSITIVE)
        await self.bot.api.messages.send(
            user_id=user_id,
            message="Чтобы пользоваться ботом, необходимо принять условия. Нажмите кнопку ниже.",
            keyboard=keyboard.get_json(),
            random_id=random.randint(0, 2 ** 31 - 1)
        )

    async def agree_terms_handler(self, message: Message):
        user_id = message.from_id
        await self._mark_agreed(user_id)
        await message.answer(
            "Спасибо! Теперь вы можете пользоваться ботом.\n"
            "Нажмите «🛒 Новый заказ» для начала.",
            keyboard=self.main_menu_keyboard(user_id).get_json()
        )

    async def safe_state_delete(self, peer_id: int):
        """Безопасно удаляет состояние."""
        try:
            await self.bot.state_dispenser.delete(peer_id)
        except KeyError:
            pass
        except Exception as e:
            logging.error(f"Error deleting state: {e}")

    async def is_order_active(self, order_id: int) -> bool:
        """Проверяет, находится ли заказ в статусе waiting_payment."""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-api-Key": INTERNAL_API_KEY}
                async with session.get(f"{API_URL}/orders/{order_id}", headers=headers) as resp:
                    if resp.status == 200:
                        order = await resp.json()
                        return order.get('status') == 'waiting_payment'
        except Exception as e:
            logging.error(f"Error checking order status: {e}")
        return False

    async def get_active_order_id(self, user_id: int) -> int | None:
        """Возвращает ID активного заказа (waiting_payment) или None."""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": INTERNAL_API_KEY}
                async with session.get(f"{API_URL}/users/{user_id}/active-order", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            return data["ID"]
        except Exception as e:
            logging.error(f"Error getting active order for user {user_id}: {e}")
        return None

    async def cancel_order_via_api(self, order_id: int):
        """Отменяет заказ через API."""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-api-Key": INTERNAL_API_KEY}
                await session.post(f"{API_URL}/admin/orders/{order_id}/cancel", headers=headers)
                logging.info(f"Order {order_id} cancelled via API")
        except Exception as e:
            logging.error(f"Error cancelling order {order_id}: {e}")

    async def delete_payment_message(self, user_id: int):
        """Удаляет сообщение с платёжной ссылкой."""
        logging.info(f"delete_payment_message called for user {user_id}, keys: {list(self.payment_messages.keys())}")
        if user_id in self.payment_messages:
            try:
                await self.bot.api.messages.delete(
                    message_ids=[self.payment_messages[user_id]],
                    delete_for_all=True
                )
                logging.info(f"Deleted payment message for user {user_id}")
                del self.payment_messages[user_id]
            except Exception as e:
                logging.error(f"Failed to delete payment message: {e}")
        else:
            logging.warning(f"Payment message for user {user_id} not found")

    async def start_order_timer(self, user_id: int):
        current_task = asyncio.current_task()
        try:
            logging.info(f"[TIMER START] user {user_id}")
            await asyncio.sleep(600)
            logging.info(f"[TIMER WAKE] user {user_id}")
            if self.order_timers.get(user_id) != current_task:
                logging.info(f"[TIMER SKIP OLD] user {user_id}")
                return
            state = await self.bot.state_dispenser.get(user_id)
            active_states = ['shop_selection', 'file_processing', 'color_selection', 'comment', 'confirmation']
            if state:
                state_str = str(state.state)
                if ':' in state_str:
                    state_str = state_str.split(':', 1)[1]
                if state_str in active_states:
                    logging.info(f"[TIMER FIRED] user {user_id}, state={state_str}")
                    await self.bot.api.messages.send(
                        user_id=user_id,
                        message="⌛ Время оформления заказа истекло. Чтобы начать новый заказ, нажмите кнопку '🛒 Новый заказ'.",
                        keyboard=self.main_menu_keyboard(user_id).get_json(),
                        random_id=random.randint(0, 2 ** 31 - 1)
                    )
                    await self.cleanup_local_data(user_id)
            else:
                logging.info(f"[TIMER NO STATE] user {user_id}")
                await self.cleanup_local_data(user_id)
            self.order_timers.pop(user_id, None)
        except asyncio.CancelledError:
            logging.info(f"[TIMER CANCELLED] user {user_id}")
        except Exception as e:
            logging.error(f"[TIMER ERROR] user {user_id}: {e}", exc_info=True)
            self.order_timers.pop(user_id, None)

    async def cancel_timer(self, user_id: int):
        task = self.order_timers.pop(user_id, None)
        if task:
            task.cancel()
            logging.info(f"[TIMER CANCEL] user {user_id}")

    async def cleanup_local_data(self, user_id: int):
        """Очищает локальные данные пользователя (временный файл, состояние), НЕ отменяет заказ."""
        # Удаляем временный файл
        user_data = self.user_data.get(user_id, {})
        temp_file = user_data.get('temp_file')
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)
        self.user_data.pop(user_id, None)
        await self.safe_state_delete(user_id)

    async def show_main_menu(self, message: Message):
        """Отправляет главное меню."""
        user_id = message.from_id
        await message.answer(
            "Главное меню:",
            keyboard=self.main_menu_keyboard(user_id).get_json()
        )

    async def start_new_order_process(self, message: Message):
        """Запускает процесс выбора магазина, если нет активного заказа."""
        user_id = message.from_id

        # Проверяем наличие активного заказа в БД
        if await self.has_active_order(user_id):
            await message.answer(
                "❌ У вас есть активный заказ, ожидающий оплаты.\n"
                "Сначала дождитесь завершения оплаты или отмените заказ кнопкой «❌ Отменить».",
                keyboard=self.cancel_keyboard().get_json()
            )
            return

        # Отменяем старый таймер и очищаем локальные данные
        await self.cancel_timer(user_id)
        await self.cleanup_local_data(user_id)

        # Получаем список магазинов
        async with aiohttp.ClientSession() as session:
            headers = {"x-api-key": INTERNAL_API_KEY}
            async with session.get(f"{API_URL}/shops", headers=headers) as resp:
                if resp.status != 200:
                    await message.answer("❌ Ошибка загрузки магазинов")
                    await self.show_main_menu(message)
                    return
                shops = await resp.json()

        # Сохраняем список магазинов
        self.user_data[user_id] = {'shops_list': shops}

        # Создаём клавиатуру
        keyboard = Keyboard(one_time=True)
        for shop in shops:
            keyboard.add(Text(shop['name']), color=KeyboardButtonColor.PRIMARY)
            keyboard.row()
        keyboard.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)

        await message.answer("🏪 Выберите точку печати из списка:", keyboard=keyboard.get_json())
        await self.bot.state_dispenser.set(user_id, States.SHOP_SELECTION)
        timer_task = asyncio.create_task(self.start_order_timer(user_id))
        self.order_timers[user_id] = timer_task
        logging.info(f"Order timer started for user {user_id}")

    async def upload_photo_to_vk(self, photo_data: bytes) -> str:
        """Загружает фото в VK и возвращает attachment."""
        import tempfile
        upload_server = await self.bot.api.photos.get_messages_upload_server()
        upload_url = upload_server.upload_url

        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            tmp.write(photo_data)
            tmp_path = tmp.name

        try:
            with open(tmp_path, 'rb') as f:
                form_data = aiohttp.FormData()
                form_data.add_field('photo', f, filename='photo.jpg')
                async with aiohttp.ClientSession() as session:
                    async with session.post(upload_url, data=form_data) as resp:
                        upload_result = await resp.json()

            saved_photos = await self.bot.api.photos.save_messages_photo(
                photo=upload_result['photo'],
                server=upload_result['server'],
                hash=upload_result['hash']
            )
            photo = saved_photos[0]
            return f"photo{photo.owner_id}_{photo.id}"
        finally:
            os.unlink(tmp_path)

    async def has_active_order(self, user_id: int) -> bool:
        """Проверяет в БД, есть ли у пользователя заказ в статусе waiting_payment."""
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": INTERNAL_API_KEY}
            async with session.get(f"{API_URL}/users/{user_id}/active-order", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data is not None
        return False

    async def cancel_order_no_message(self, message: Message):
        user_id = message.from_id
        order_id = self.active_orders.pop(user_id, None)
        if not order_id:
            order_id = await self.get_active_order_id(user_id)
        if order_id:
            await self.cancel_order_via_api(order_id)
            await self.delete_payment_message(user_id)
        await self.cleanup_local_data(user_id)

    # --------------------------
    # Клавиатуры
    # --------------------------
    def main_menu_keyboard(self, user_id: int = None):
        """Главное меню."""
        kb = Keyboard(inline=False)
        kb.add(Text("🛒 Новый заказ"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("ℹ️ Помощь"))
        if user_id and self.is_admin(user_id):
            kb.row()
            kb.add(Text("📢 Рассылка"), color=KeyboardButtonColor.PRIMARY)
        return kb

    def cancel_keyboard(self):
        """Клавиатура только с кнопкой 'Отменить'."""
        kb = Keyboard(inline=False)
        kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)
        return kb

    def after_cancel_keyboard(self):
        """Клавиатура, показываемая после отмены заказа."""
        kb = Keyboard(inline=False)
        kb.add(Text("❌ Отменить оплату"), color=KeyboardButtonColor.SECONDARY)
        return kb

    def color_keyboard(self):
        """Клавиатура для выбора цвета печати + отмена."""
        kb = Keyboard(inline=False)
        kb.add(Text("Черно-белая"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("Цветная"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)
        return kb

    def comment_keyboard(self):
        """Клавиатура для комментария + отмена."""
        kb = Keyboard(inline=False)
        kb.add(Text("Без комментария"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)
        return kb

    def confirm_keyboard(self):
        """Клавиатура подтверждения + отмена."""
        kb = Keyboard(inline=False)
        kb.add(Text("💳 Оплатить"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)
        return kb

    def get_shops_keyboard(self, user_id: int):
        """Возвращает клавиатуру выбора магазина на основе сохранённого списка."""
        user_data = self.user_data.get(user_id, {})
        shops = user_data.get('shops_list')
        if not shops:
            return None
        kb = Keyboard(one_time=True)
        for shop in shops:
            kb.add(Text(shop['name']), color=KeyboardButtonColor.PRIMARY)
            kb.row()
        kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.SECONDARY)
        return kb

    # --------------------------
    # Обработчики сообщений
    # --------------------------
    async def start_handler(self, message: Message):
        """Приветственное сообщение и главное меню."""
        user_id = message.from_id
        full_name = await self.get_user_full_name(user_id)
        if not await self.ensure_onboarding(user_id):
            return

        if await self.has_active_order(user_id):
            await message.answer(
                "У вас есть активный заказ, ожидающий оплаты.\n"
                "Сначала дождитесь завершения оплаты или отмените заказ кнопкой «❌ Отменить».",
                keyboard=self.cancel_keyboard().get_json()
            )
            return

        await self.cleanup_local_data(user_id)

        await message.answer(
            f"Привет, {full_name}! Рады приветствовать тебя на нашем сервисе по печати документов в любое удобное время! "
            f"Чтобы начать новый заказ, нажмите кнопку '🛒 Новый заказ'",
            keyboard=self.main_menu_keyboard(user_id).get_json()
        )

    async def new_order_handler(self, message: Message):
        """Обработчик кнопки 'Новый заказ'."""
        user_id = message.from_id
        if not await self.ensure_onboarding(user_id):
            return
        await self.start_new_order_process(message)

    async def reset_handler(self, message: Message):
        user_id = message.from_id
        order_id = self.active_orders.pop(user_id, None)
        if not await self.ensure_onboarding(user_id):
            return

        if not order_id:
            order_id = await self.get_active_order_id(user_id)

        if order_id:
            await self.cancel_order_via_api(order_id)
            await self.delete_payment_message(user_id)
        else:
            await message.answer(
                "❌ Заказ отменен",
                keyboard=self.main_menu_keyboard(user_id).get_json()
            )
        await self.cleanup_local_data(user_id)

    async def help_handler(self, message: Message):
        """Обработчик кнопки 'Помощь'."""
        await message.answer(
            "Если у вас возникли вопросы, проблемы с заказом или вам просто нужна консультация — обратитесь в нашу службу поддержки.\n\n"
            "📞 Контакты:\n"
            "• Email: send-to-print-and-pick-up@yandex.ru\n"
            "❗ Время ответа от поддержки может занимать до 72 часов\n"
            "Мы обязательно вам поможем! 😊\n\n"
            "📚 Документация сервиса Send to print and pick up: https://disk.yandex.ru/d/Q-1xYZuSQFZNYA"
        )

    async def broadcast_start(self, message: Message):
        user_id = message.from_id
        if not self.is_admin(user_id):
            return
        await self.bot.state_dispenser.set(user_id, BroadcastStates.WAITING_MESSAGE)
        await message.answer(
            "📢 Режим рассылки\nОтправьте текст сообщения (можно несколько строк).\nДля отмены нажмите '❌ Отменить'.",
            keyboard=self.cancel_keyboard().get_json()
        )

    async def broadcast_waiting_message(self, message: Message):
        user_id = message.from_id
        peer_id = message.peer_id
        text = message.text or ""
        photos = []

        if message.text == "❌ Отменить":
            await self.bot.state_dispenser.delete(user_id)
            await message.answer("Рассылка отменена.", keyboard=self.main_menu_keyboard(user_id).get_json())
            return

        if message.attachments:
            for att in message.attachments:
                if att.type == "photo":
                    photo = att.photo
                    max_size = max(photo.sizes, key=lambda s: s.width * s.height)
                    file_url = max_size.url
                    async with aiohttp.ClientSession() as session:
                        async with session.get(file_url) as resp:
                            photo_data = await resp.read()
                    attachment = await self.upload_photo_to_vk(photo_data)
                    photos.append(attachment)
            if len(photos) > 10:
                await message.answer("❌ Слишком много фото (максимум 10). Попробуйте ещё раз.")
                return

        self.broadcast_data[peer_id] = {'text': text, 'photos': photos}

        preview = "👇 Превью рассылки:\n"
        if text:
            preview += f"\n{text}\n"
        if photos:
            preview += f"\n📷 Фото ({len(photos)} шт.)\n"
        else:
            preview += "\n(без фото)\n"
        preview += "\nОтправить это сообщение всем пользователям?"

        kb = Keyboard(inline=False)
        kb.add(Text("Подтвердить"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("Отмена"), color=KeyboardButtonColor.SECONDARY)

        await message.answer(preview, keyboard=kb)
        await self.bot.state_dispenser.set(user_id, BroadcastStates.CONFIRM)

    async def broadcast_confirm(self, message: Message):
        user_id = message.from_id
        peer_id = message.peer_id
        text = message.text

        if text == "Отмена":
            self.broadcast_data.pop(peer_id, None)
            await self.bot.state_dispenser.delete(user_id)
            await message.answer("Рассылка отменена.", keyboard=self.main_menu_keyboard(user_id).get_json())
            return

        if text != "Подтвердить":
            await message.answer("❌ Пожалуйста, используйте кнопки для подтверждения.")
            return

        data = self.broadcast_data.get(peer_id)
        if not data:
            await message.answer("❌ Ошибка: данные не найдены. Начните заново.")
            await self.bot.state_dispenser.delete(user_id)
            return

        msg_text = data['text']
        photos = data['photos']

        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": INTERNAL_API_KEY}
            async with session.get(f"{API_URL}/users/agreed-ids?platform=vk", headers=headers) as resp:
                if resp.status != 200:
                    await message.answer("❌ Ошибка API.", keyboard=self.main_menu_keyboard(user_id).get_json())
                    return
                user_ids = await resp.json()

        total = len(user_ids)
        if total == 0:
            await message.answer("❌ Нет получателей для рассылки.",
                                 keyboard=self.main_menu_keyboard(user_id).get_json())
            return

        await message.answer(f"⏳ Начинаю рассылку для {total} получателей...")

        sent = 0
        failed = 0
        for uid in user_ids:
            try:
                params = {
                    "user_id": uid,
                    "message": msg_text,
                    "random_id": random.randint(0, 2 ** 31 - 1)
                }
                if photos:
                    params["attachment"] = ",".join(photos[:10])
                await self.bot.api.messages.send(**params)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logging.error(f"Failed to send to VK user {uid}: {e}")
                failed += 1

        await message.answer(
            f"📊 Итог рассылки:\n✅ Успешно: {sent}\n❌ Ошибок: {failed}",
            keyboard=self.main_menu_keyboard(user_id).get_json()
        )

        self.broadcast_data.pop(peer_id, None)
        await self.bot.state_dispenser.delete(user_id)

    async def unknown_message_handler(self, message: Message):
        """Обработчик для всех сообщений, не попавших в другие обработчики."""
        user_id = message.from_id
        await message.answer(
            "❌ Неизвестная команда.\n"
            "Пожалуйста, используйте кнопки главного меню.",
            keyboard=self.main_menu_keyboard(user_id).get_json()
        )

    # --------------------------
    # Шаг 1: Выбор магазина
    # --------------------------
    async def process_shop_selection(self, message: Message):
        user_id = message.from_id
        shop_name = message.text
        if not await self.ensure_onboarding(user_id):
            return

        if shop_name == "❌ Отменить":
            await self.reset_handler(message)
            return

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"x-api-key": INTERNAL_API_KEY}
                async with session.get(f"{API_URL}/shops/{shop_name}", headers=headers) as resp:
                    if resp.status != 200:
                        keyboard = self.get_shops_keyboard(user_id)
                        if keyboard:
                            await message.answer(
                                "❌ Точка не найдена. Пожалуйста, выберите точку из списка:",
                                keyboard=keyboard.get_json()
                            )
                        else:
                            await message.answer("❌ Точка не найдена. Нажмите '🛒 Новый заказ' для выбора заново")
                            await self.safe_state_delete(user_id)
                            await self.show_main_menu(message)
                        return
                    shop = await resp.json()
        except Exception as e:
            logging.error(f"Error fetching shop: {e}")
            await message.answer("❌ Ошибка при получении информации о точке. Попробуйте позже")
            await self.safe_state_delete(user_id)
            await self.show_main_menu(message)
            return

        self.user_data[user_id]['shop'] = shop
        self.user_data[user_id].pop('shops_list', None)

        response = (
            f"🏪 Выбрана точка: {shop['name']}\n"
            f"⌚ Время работы: {shop['w_hours']}\n"
            f"📍 Адрес: {shop['address']}\n"
            f"💰 Цены:\n"
            f"• Черно-белая: {shop['price_bw']:.2f} руб/стр\n"
            f"• Цветная: {shop['price_cl']:.2f} руб/стр\n\n"
            f"📎 Отправьте один PDF, DOC, DOCX, PNG, JPEG, JPG файл или одну фотографию размером не более 20 МБ для расчета стоимости"
        )
        await message.answer(response, keyboard=self.cancel_keyboard().get_json())
        await self.bot.state_dispenser.set(user_id, States.FILE_PROCESSING)

    # --------------------------
    # Шаг 2: Приём файла
    # --------------------------
    async def process_file(self, message: Message):
        user_id = message.from_id
        if not await self.ensure_onboarding(user_id):
            return

        if message.text == "❌ Отменить":
            await self.reset_handler(message)
            return

        attachments = message.attachments
        if not attachments:
            await message.answer("❌ Пожалуйста, отправьте файл (документ или фотографию)", keyboard=self.cancel_keyboard().get_json())
            return

        if len(attachments) > 1:
            await message.answer("❌ Отправляйте только один файл за раз. Пожалуйста, пришлите один документ или одну фотографию", keyboard=self.cancel_keyboard().get_json())
            return

        attachment = attachments[0]
        file_type = attachment.type

        try:
            if file_type == "doc":
                doc = attachment.doc
                file_url = doc.url
                filename = doc.title
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext not in ('.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'):
                    await message.answer("❌ Поддерживаются только форматы: PDF, DOC, DOCX, PNG, JPEG, JPG", keyboard=self.cancel_keyboard().get_json())
                    return
            elif file_type == "photo":
                sizes = attachment.photo.sizes
                largest = max(sizes, key=lambda s: s.width * s.height)
                file_url = largest.url
                filename = f"photo_{user_id}_{uuid.uuid4()}.jpg"
                file_ext = ".jpg"
            else:
                await message.answer("❌ Неподдерживаемый тип вложения. Отправьте документ или фотографию", keyboard=self.cancel_keyboard().get_json())
                return
        except Exception as e:
            logging.error(f"Error parsing attachment: {e}")
            await message.answer("❌ Ошибка при обработке вложения. Попробуйте ещё раз", keyboard=self.cancel_keyboard().get_json())
            return

        processing_msg = await message.answer("⏳ Файл обрабатывается, подождите пожалуйста...")

        try:
            file_content = await download_file_from_url(file_url)
            if len(file_content) > MAX_FILE_SIZE:
                raise ValueError("Файл слишком большой. Максимальный размер — 20 МБ")

            temp_name = f"temp_{uuid.uuid4()}{file_ext}"
            temp_path = os.path.join(UPLOAD_FOLDER, temp_name)
            async with aiofiles.open(temp_path, 'wb') as f:
                await f.write(file_content)

            if not await is_file_safe(temp_path, file_ext):
                raise ValueError("Файл поврежден или имеет неверный формат")

            if not await scan_file(temp_path):
                raise ValueError("Файл содержит вирусы и был удален")

            pages = await get_page_count(temp_path, file_ext)
            if pages is None or pages < 1:
                raise ValueError("Не удалось определить количество страниц")
            if pages > 500:
                raise ValueError("Слишком много страниц")

            self.user_data[user_id].update({
                "temp_file": temp_path,
                "pages": pages,
                "file_extension": file_ext[1:],
                "filename": filename
            })

            await message.answer(
                f"📄 Файл успешно обработан!\nКоличество страниц: {pages}\nВыберите тип печати:",
                keyboard=self.color_keyboard().get_json()
            )
            await self.bot.state_dispenser.set(user_id, States.COLOR_SELECTION)

        except Exception as e:
            logging.error(f"File processing error: {e}")
            await message.answer(f"❌ Ошибка обработки: {str(e)}", keyboard=self.cancel_keyboard().get_json())
            if "temp_path" in locals() and temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        finally:
            try:
                await self.bot.api.messages.delete(
                    message_ids=[processing_msg.message_id],
                    delete_for_all=True
                )
            except Exception as e:
                logging.error(f"Error deleting processing message: {e}")

    # --------------------------
    # Шаг 3: Выбор цвета печати
    # --------------------------
    async def process_color(self, message: Message):
        user_id = message.from_id
        if not await self.ensure_onboarding(user_id):
            return

        if message.text == "❌ Отменить":
            await self.reset_handler(message)
            return

        user_data = self.user_data.get(user_id, {})
        if not user_data.get('shop'):
            await message.answer("❌ Ошибка: данные не найдены. Нажмите '🛒 Новый заказ' для начала")
            await self.safe_state_delete(user_id)
            await self.show_main_menu(message)
            return

        color_text = message.text
        if color_text not in ["Черно-белая", "Цветная"]:
            await message.answer("❌ Неверный тип печати! Выберите из предложенных вариантов", keyboard=self.color_keyboard().get_json())
            return

        color = color_text.lower()
        shop = user_data["shop"]
        price = shop["price_bw"] if color == "черно-белая" else shop["price_cl"]
        total_price = round(price * user_data["pages"], 2)

        user_data["color"] = color
        user_data["price"] = total_price
        self.user_data[user_id] = user_data

        await message.answer(
            "📝 Введите комментарий к заказу или нажмите кнопку ниже:",
            keyboard=self.comment_keyboard().get_json()
        )
        await self.bot.state_dispenser.set(user_id, States.COMMENT)

    # --------------------------
    # Шаг 4: Комментарий
    # --------------------------
    async def process_comment(self, message: Message):
        user_id = message.from_id
        user_data = self.user_data.get(user_id, {})
        if not await self.ensure_onboarding(user_id):
            return

        if message.text == "❌ Отменить":
            await self.reset_handler(message)
            return

        if message.text == "Без комментария":
            comment = ''
        else:
            comment = message.text
            if len(comment) > 255:
                await message.answer("❌ Комментарий слишком длинный! Максимальная длина — 255 символов", keyboard=self.comment_keyboard().get_json())
                return

        user_data["comment"] = comment
        self.user_data[user_id] = user_data

        shop = user_data["shop"]
        response = (
            f"🔍 Подтвердите заказ:\n"
            f"• Точка: {shop['name']} по адресу {shop['address']}\n"
            f"• Страниц: {user_data['pages']}\n"
            f"• Тип печати: {user_data['color']}\n"
            f"• Стоимость: {user_data['price']:.2f} руб\n"
            f"• Комментарий: {comment if comment else 'нет'}\n"
            f"Если все верно — нажмите кнопку '💳 Оплатить'"
        )
        await message.answer(response, keyboard=self.confirm_keyboard().get_json())
        await self.bot.state_dispenser.set(user_id, States.CONFIRMATION)

    # --------------------------
    # Шаг 5: Подтверждение и создание заказа
    # --------------------------
    async def process_confirmation(self, message: Message):
        user_id = message.from_id
        user_data = self.user_data.get(user_id, {})
        if not await self.ensure_onboarding(user_id):
            return

        if message.text == "❌ Отменить":
            await self.reset_handler(message)
            return

        if message.text != "💳 Оплатить":
            await message.answer("⚠️ Пожалуйста, используйте кнопки для оплаты", keyboard=self.confirm_keyboard().get_json())
            return

        # Защита от повторного нажатия
        if user_data.get('processing'):
            await message.answer("⏳ Ссылка создается, подождите...")
            return

        await self.cancel_timer(user_id)

        # Устанавливаем флаг обработки
        user_data['processing'] = True
        self.user_data[user_id] = user_data

        check_code = random.randint(1000, 9999)
        processing_msg = await message.answer("⏳ Ссылка на оплату формируется, подождите...")

        order_id = None
        try:
            async with aiohttp.ClientSession() as session:
                form_data = aiohttp.FormData()
                form_data.add_field("ID_shop", str(user_data["shop"]["ID_shop"]))
                form_data.add_field("price", str(user_data["price"]))
                form_data.add_field("pages", str(user_data["pages"]))
                form_data.add_field("color", user_data["color"])
                form_data.add_field("user_id", str(user_id))
                form_data.add_field("note", user_data.get("comment", ""))
                form_data.add_field("file_extension", user_data["file_extension"])
                form_data.add_field('platform', 'vk')
                form_data.add_field("con_code", str(check_code))

                with open(user_data["temp_file"], "rb") as f:
                    form_data.add_field("file", f.read(), filename=user_data["filename"])

                headers = {"x-api-key": INTERNAL_API_KEY}
                async with session.post(f"{API_URL}/orders", data=form_data, headers=headers) as resp:
                    if resp.status != 201:
                        error_text = await resp.text()
                        logging.error(f"Order creation failed: {resp.status}, {error_text}")
                        raise Exception("Ошибка создания заказа")
                    order_data = await resp.json()
                    order_id = order_data["order_id"]

                async with session.post(
                    f"{API_URL}/payments/create",
                    json={"order_id": order_id},
                    headers=headers
                ) as payment_resp:
                    if payment_resp.status != 200:
                        error_text = await payment_resp.text()
                        logging.error(f"Payment creation failed: {payment_resp.status}, {error_text}")
                        raise Exception("Ошибка создания платежа")
                    payment_info = await payment_resp.json()
                    confirmation_url = payment_info.get("confirmation_url")
                    if not confirmation_url:
                        raise Exception("Неверный ответ от платёжного шлюза")

            sent_msg = await message.answer(
                f"💳 Для завершения заказа перейдите по ссылке:\n{confirmation_url}\n"
                f"❗ Ссылка будет действительна в течение 10 минут",
                keyboard=self.after_cancel_keyboard().get_json()
            )

            self.payment_messages[user_id] = sent_msg.message_id
            self.active_orders[user_id] = order_id

            if user_data["temp_file"] and os.path.exists(user_data["temp_file"]):
                os.remove(user_data["temp_file"])

            self.user_data.pop(user_id, None)
            await self.safe_state_delete(user_id)

        except Exception as e:
            logging.error(f"Payment creation error: {traceback.format_exc()}")
            if order_id:
                await self.cancel_order_via_api(order_id)
            await message.answer("❌ Произошла ошибка при создании заказа/платежа. Попробуйте позже", keyboard=self.main_menu_keyboard(user_id).get_json())
            if user_id in self.user_data:
                self.user_data[user_id].pop('processing', None)
        finally:
            try:
                await self.bot.api.messages.delete(
                    message_ids=[processing_msg.message_id],
                    delete_for_all=True
                )
            except Exception as e:
                logging.error(f"Error deleting processing message: {e}")

    # --------------------------
    # Обработчик callback-событий
    # --------------------------
    async def callback_handler(self, event: GroupTypes.MessageEvent):
        payload = event.object.payload
        if not payload:
            return
        # Можно добавить обработку, если понадобятся inline-кнопки
        await event.answer()

    # --------------------------
    # HTTP сервер для уведомлений от API
    # --------------------------
    async def handle_notify(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Internal-Key") != INTERNAL_API_KEY:
            return web.Response(status=403)

        data = await request.json()
        user_id = data.get("user_id")
        order_id = data.get("order_id")
        status = data.get("status")
        address = data.get("address")
        con_code = data.get("con_code")

        if user_id is not None:
            user_id = int(user_id)

        logging.info(f"Notify received: user_id={user_id}, order_id={order_id}, status={status}")

        if status in ('paid', 'expired', 'canceled', 'ready', 'completed'):
            logging.info(f"Attempting to delete payment message for user {user_id}")
            await self.delete_payment_message(user_id)
            if user_id in self.active_orders and self.active_orders.get(user_id) == order_id:
                self.active_orders.pop(user_id, None)

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
        else:
            return web.Response(status=200)

        try:
            await self.bot.api.messages.send(user_id=user_id, message=text,
                keyboard=self.main_menu_keyboard(user_id).get_json(), random_id=0)
        except Exception as e:
            logging.error(f"Failed to send VK message: {e}")

        return web.Response(status=200)

    async def run_http_server(self):
        app = web.Application()
        app.router.add_post('/notify', self.handle_notify)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', 8002)
        await site.start()
        logging.info("VK notification HTTP server started on port 8002")

    # --------------------------
    # Настройка обработчиков
    # --------------------------
    def setup_handlers(self):
        self.bot.on.private_message(text=["Начать", "начать"])(self.start_handler)
        self.bot.on.private_message(text=["🛒 Новый заказ"])(self.new_order_handler)
        self.bot.on.private_message(text=["ℹ️ Помощь"])(self.help_handler)
        self.bot.on.private_message(text=["📢 Рассылка"])(self.broadcast_start)
        self.bot.on.private_message(text=["❌ Отменить"])(self.reset_handler)
        self.bot.on.private_message(text=["❌ Отменить оплату"])(self.cancel_order_no_message)
        self.bot.on.private_message(text=["✅ Согласен"])(self.agree_terms_handler)

        self.bot.on.private_message(state=States.SHOP_SELECTION)(self.process_shop_selection)
        self.bot.on.private_message(state=States.FILE_PROCESSING)(self.process_file)
        self.bot.on.private_message(state=States.COLOR_SELECTION)(self.process_color)
        self.bot.on.private_message(state=States.COMMENT)(self.process_comment)
        self.bot.on.private_message(state=States.CONFIRMATION)(self.process_confirmation)
        self.bot.on.private_message(state=BroadcastStates.WAITING_MESSAGE)(self.broadcast_waiting_message)
        self.bot.on.private_message(state=BroadcastStates.CONFIRM)(self.broadcast_confirm)

        self.bot.on.raw_event(GroupEventType.MESSAGE_EVENT, GroupTypes.MessageEvent)(self.callback_handler)
        self.bot.on.private_message()(self.unknown_message_handler)

    # --------------------------
    # Запуск бота
    # --------------------------
    def run_forever(self):
        self.bot.loop_wrapper.add_task(self.run_http_server())
        self.bot.run_forever()


if __name__ == "__main__":
    if not VK_TOKEN:
        logging.error("VK_BOT_TOKEN not set in .env")
        exit(1)

    bot = VKPrintBot(VK_TOKEN)
    bot.run_forever()