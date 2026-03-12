import os
import logging
import asyncio
import aiohttp
import aiofiles
import uuid
import traceback
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ContentType
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.DEBUG,
    filename='bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL")
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


class Form(StatesGroup):
    file_processing = State()
    confirmation = State()


bot = Bot(token=API_TOKEN)
dp = Dispatcher()
timers = {}
confirmation_timers = {}


async def cleanup_order_data(user_data: dict):
    try:
        if 'order_id' in user_data:
            async with aiohttp.ClientSession() as session:
                await session.delete(f"{API_URL}/orders/{user_data['order_id']}")
    except Exception as e:
        logging.error(f"Ошибка очистки: {str(e)}")


async def start_order_timer(chat_id: int, state: FSMContext):
    try:
        await asyncio.sleep(600)
        if chat_id in timers:
            user_data = await state.get_data()
            await cleanup_order_data(user_data)
            await bot.send_message(chat_id, "❌ Время оформления заказа истекло, ваш заказ отменен",
                                   reply_markup=types.ReplyKeyboardRemove())
            await state.clear()
            del timers[chat_id]
    except asyncio.CancelledError:
        logging.info("10-минутный таймер отменен")


async def confirmation_timeout(chat_id: int, state: FSMContext):
    try:
        await asyncio.sleep(60)
        if chat_id in confirmation_timers:
            user_data = await state.get_data()
            await cleanup_order_data(user_data)
            await bot.send_message(chat_id, "❌ Время подтверждения истекло, ваш заказ отменен",
                                   reply_markup=types.ReplyKeyboardRemove())
            await state.clear()
            del confirmation_timers[chat_id]
    except asyncio.CancelledError:
        logging.info("1-минутный таймер отменен")


async def save_temp_file(file_content: bytes, original_filename: str, ext: str) -> str:
    """Сохраняет временный файл и возвращает путь"""
    temp_name = f"temp_{uuid.uuid4()}{ext}"
    temp_path = os.path.join(UPLOAD_FOLDER, temp_name)
    async with aiofiles.open(temp_path, 'wb') as f:
        await f.write(file_content)
    return temp_path


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя в нашем сервисе по распечатке документов!"
        f" Чтобы начать новый заказ, используйте команду /new_order.",
        reply_markup=types.ReplyKeyboardRemove()
    )


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    try:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        if message.chat.id in confirmation_timers:
            confirmation_timers[message.chat.id].cancel()
            del confirmation_timers[message.chat.id]

        user_data = await state.get_data()
        temp_file = user_data.get('temp_file')
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

        await state.clear()
        await message.answer(
            "🔄 Все данные сброшены. Вы можете начать новый заказ с помощью /new_order",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception as e:
        logging.error(f"Ошибка в reset: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка при сбросе")


@dp.message(Command("new_order"))
async def cmd_new_order(message: types.Message, state: FSMContext):
    # Отмена предыдущих таймеров
    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]
    if message.chat.id in confirmation_timers:
        confirmation_timers[message.chat.id].cancel()
        del confirmation_timers[message.chat.id]

    # Очистка временных файлов из предыдущего состояния
    user_data = await state.get_data()
    temp_file = user_data.get('temp_file')
    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
        except Exception as e:
            logging.error(f"Ошибка удаления файла: {str(e)}")

    await state.clear()

    # Проверка активных заказов пользователя
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/orders/count/{message.chat.id}") as resp:
            if resp.status != 200:
                await message.answer("❌ Ошибка проверки заказов")
                return
            data = await resp.json()
            if data["active_orders"] >= 2:
                await message.answer(
                    "❌ Максимальное количество заказов на пользователя: 2.\n"
                    "Пожалуйста, для начала заберите предыдущие заказы.",
                    reply_markup=types.ReplyKeyboardRemove()
                )
                return

    # Отправляем фиксированное сообщение с информацией о точке печати
    await message.answer(
        "🏪 Точка печати: Фундаментальная библиотека\n"
        "⌚ Время работы: пн-чт 9-16, пт 9-15\n"
        "📍 Адрес: проспект Гагарина, 23к1, каб. 243-2\n\n"
        "📎 Отправьте один PDF, DOC, DOCX, PNG, JPEG, JPG файл или одну фотографию размером не более 20 МБ для расчета стоимости\n"
        "Используйте /reset для отмены заказа.",
        reply_markup=types.ReplyKeyboardRemove()
    )
    timers[message.chat.id] = asyncio.create_task(start_order_timer(message.chat.id, state))
    await state.set_state(Form.file_processing)


@dp.message(Form.file_processing, F.content_type == ContentType.DOCUMENT)
async def process_document(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    if user_data.get('temp_file'):
        await message.answer(
            "❌ Вы уже отправили файл. Дождитесь обработки или отмените текущий заказ командой /reset.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    if message.media_group_id:
        await message.answer(
            "❌ Пожалуйста, отправляйте файлы по одному. Сначала дождитесь обработки текущего файла.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    processing_msg = await message.answer("⏳ Файл обрабатывается, подождите пожалуйста...")

    try:
        file_info = await bot.get_file(message.document.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к файлу")

        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Ошибка HTTP {resp.status}")
                file_content = await resp.read()

        filename = message.document.file_name or "unnamed_file"
        file_ext = os.path.splitext(filename)[1].lower()
        allowed_ext = ('.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg')
        if file_ext not in allowed_ext:
            raise ValueError("Поддерживаются только форматы: PDF, DOC, DOCX, PNG, JPEG, JPG")

        temp_path = await save_temp_file(file_content, filename, file_ext)

        if not os.path.exists(temp_path):
            raise ValueError("Не удалось сохранить файл на диск")

        await state.update_data({
            'temp_file': temp_path,
            'filename': filename
        })

        # Переход к подтверждению
        await show_confirmation(message, state)

    except ValueError as ve:
        await cancel_order(message, state, str(ve))
    except Exception as e:
        logging.error(f"Критическая ошибка: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка. Используйте /new_order", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
    finally:
        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass


@dp.message(Form.file_processing, F.content_type == ContentType.PHOTO)
async def process_photo(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    if user_data.get('temp_file'):
        await message.answer(
            "❌ Вы уже отправили файл. Дождитесь обработки или отмените текущий заказ командой /reset.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    if message.media_group_id:
        await message.answer(
            "❌ Пожалуйста, отправляйте фото по одному. Сначала дождитесь обработки текущего.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    processing_msg = await message.answer("⏳ Фото обрабатывается, подождите пожалуйста...")

    try:
        # Берём самое большое фото (последнее в массиве)
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к фото")

        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Ошибка HTTP {resp.status}")
                file_content = await resp.read()

        # Генерируем имя файла (можно использовать дату или просто photo_id)
        filename = f"photo_{photo.file_id}.jpg"
        file_ext = '.jpg'  # Telegram хранит фото в JPEG
        allowed_ext = ('.jpg', '.jpeg', '.png')  # фото всегда jpeg, но оставим
        if file_ext not in allowed_ext:
            raise ValueError("Неподдерживаемый формат фото")

        temp_path = await save_temp_file(file_content, filename, file_ext)

        if not os.path.exists(temp_path):
            raise ValueError("Не удалось сохранить фото на диск")

        await state.update_data({
            'temp_file': temp_path,
            'filename': filename
        })

        # Переход к подтверждению
        await show_confirmation(message, state)

    except ValueError as ve:
        await cancel_order(message, state, str(ve))
    except Exception as e:
        logging.error(f"Критическая ошибка при обработке фото: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка. Используйте /new_order", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
    finally:
        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass


async def show_confirmation(message: types.Message, state: FSMContext):
    """Показывает экран подтверждения заказа"""
    user_data = await state.get_data()
    filename = user_data['filename']
    text = (f"🔍 Подтвердите заказ:\n"
            f"• Точка: Фундаментальная библиотека\n"
            f"• Адрес: проспект Гагарина, 23к1, каб. 243-2\n"
            f"• Файл: {filename}\n\n"
            f"Всё верно?")

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отменить")]],
        resize_keyboard=True
    )
    confirmation_msg = await message.answer(text, reply_markup=markup)
    await state.update_data(confirmation_msg_id=confirmation_msg.message_id)
    await state.set_state(Form.confirmation)


async def cancel_order(message: types.Message, state: FSMContext, error_text: str = None):
    """Отмена заказа с очисткой"""
    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]
    if message.chat.id in confirmation_timers:
        confirmation_timers[message.chat.id].cancel()
        del confirmation_timers[message.chat.id]
    await state.clear()
    error_msg = f"❌ Ошибка: {error_text}. Используйте /new_order" if error_text else "❌ Заказ отменен"
    await message.answer(error_msg, reply_markup=types.ReplyKeyboardRemove())


@dp.message(Form.confirmation)
async def process_confirmation(message: types.Message, state: FSMContext):
    if message.text not in ["Подтвердить", "Отменить"]:
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отменить")]],
            resize_keyboard=True
        )
        await message.answer("⚠️ Пожалуйста, используйте кнопки:", reply_markup=markup)
        return

    # Останавливаем таймеры
    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]
    if message.chat.id in confirmation_timers:
        confirmation_timers[message.chat.id].cancel()
        del confirmation_timers[message.chat.id]

    user_data = await state.get_data()
    temp_file_path = user_data.get('temp_file')

    if message.text == 'Отменить':
        await message.answer("❌ Заказ отменен", reply_markup=types.ReplyKeyboardRemove())
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as e:
                logging.error(f"Ошибка удаления файла: {str(e)}")
        await state.clear()
        return

    # Подтверждение заказа
    try:
        async with aiohttp.ClientSession() as session:
            form_data = aiohttp.FormData()
            # ID магазина всегда 1
            form_data.add_field('ID_shop', '1')
            form_data.add_field('user_id', str(message.chat.id))
            with open(temp_file_path, 'rb') as file:
                form_data.add_field('file', file.read(), filename=user_data['filename'])

            async with session.post(f"{API_URL}/orders", data=form_data) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    await message.answer(
                        f"✅ Заказ №{data['order_id']} принят! Ждем вас на точке Фундаментальная библиотека по "
                        f"адресу: проспект Гагарина, 23к1, каб. 243-2\n"
                        f"❗ Обращаем ваше внимание, что заказы хранятся 3 дня с момента создания!",
                        reply_markup=types.ReplyKeyboardRemove()
                    )
                    if temp_file_path and os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                else:
                    await message.answer("❌ Ошибка создания заказа", reply_markup=types.ReplyKeyboardRemove())
    except Exception as e:
        logging.error(f"Ошибка подтверждения: {traceback.format_exc()}")
        await message.answer("❌ Ошибка сети", reply_markup=types.ReplyKeyboardRemove())
    finally:
        await state.clear()


@dp.message(Command("my_orders"))
async def cmd_my_orders(message: types.Message):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/orders/user/{message.chat.id}") as resp:
            if resp.status != 200:
                await message.answer("❌ Ошибка получения заказов")
                return
            orders = await resp.json()

    if not orders:
        await message.answer("📭 У вас нет активных заказов.")
        return

    text = "📦 Ваши активные заказы:\n\n"
    now = datetime.now(timezone.utc)

    for order in orders:
        created_raw = order.get('created_at')
        time_left = "неизвестно"

        if created_raw:
            try:
                # Парсим ISO-строку (например, "2026-03-13T01:31:09+00:00")
                dt = datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
                dt_utc = dt.astimezone(timezone.utc)
                delta = (now - dt_utc).total_seconds()
                days_old = delta / 86400
                if days_old < 3:
                    remaining = 3 - days_old
                    if remaining < 1:
                        time_left = f"{int(remaining * 24)} ч."
                    else:
                        time_left = f"{remaining:.0f} дн."
                else:
                    time_left = "будет удалён"
            except:
                time_left = "неизвестно"

        text += (
            f"🟡 Заказ №{order['ID']}\n"
            f"🏪 Точка: {order['shop_name']}\n"
            f"📍 Адрес: {order['address']}\n"
            f"📄 Файл: {order['user_file_name']}\n"
            f"⏳ Хранится ещё: {time_left}\n\n"
        )
    await message.answer(text)


@dp.message(Form.file_processing)
async def process_file_invalid(message: types.Message):
    await message.answer(
        "❌ Пожалуйста, отправьте файл в формате PDF, DOC, DOCX, PNG, JPEG, JPG.\n"
        "Используйте /reset для отмены заказа.",
        reply_markup=types.ReplyKeyboardRemove()
    )


@dp.message()
async def handle_unknown(message: types.Message):
    await message.reply("Не понимаю тебя, попробуй повторить запрос ☺️")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())