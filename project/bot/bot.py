import os
import logging
import random
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
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.DEBUG,
    filename='bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


class Form(StatesGroup):
    shop_selection = State()
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


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя в нашем сервисе по распечатке документов!"
        f" Чтобы начать новый заказ, используйте команду /new_order.",
        reply_markup=types.ReplyKeyboardRemove()
    )


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

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/shops") as resp:
            if resp.status != 200:
                await message.answer("❌ Ошибка загрузки магазинов")
                return
            shops = await resp.json()

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=shop['name'])] for shop in shops],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("🏪 Выберите точку печати из списка:", reply_markup=markup)
    timers[message.chat.id] = asyncio.create_task(start_order_timer(message.chat.id, state))
    await state.set_state(Form.shop_selection)


@dp.message(Form.shop_selection)
async def process_shop(message: types.Message, state: FSMContext):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/shops/{message.text}") as resp:
            if resp.status != 200:
                await message.answer("❌ Точка не найдена. /new_order")
                return
            shop = await resp.json()

    await state.update_data(shop=shop)
    response = (
        f"🏪 Выбрана точка: {shop['name']}\n"
        f"⌚ Время работы: {shop['w_hours']}\n"
        f"📍 Адрес: {shop['address']}\n\n"
        f"📎 Отправьте файл (PDF, DOC, DOCX, PNG, JPEG, JPG) размером не более 20 МБ.\n"
        f"❗ Внимание! Если вы отправляете картинку, то прикрепляйте ее в виде файла!\n"
        f"Используйте /reset для отмены заказа."
    )
    await message.answer(response, reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(Form.file_processing)


@dp.message(Form.file_processing, F.content_type == ContentType.DOCUMENT)
async def process_file(message: types.Message, state: FSMContext):
    processing_msg = await message.answer("⏳ Файл обрабатывается, подождите пожалуйста...")
    temp_path = None

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

        temp_name = f"temp_{uuid.uuid4()}{file_ext}"
        temp_path = os.path.join(UPLOAD_FOLDER, temp_name)
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(file_content)

        if not os.path.exists(temp_path):
            raise ValueError("Не удалось сохранить файл на диск")

        await state.update_data({
            'temp_file': temp_path,
            'filename': filename
        })

        # Переход к подтверждению
        user_data = await state.get_data()
        shop = user_data['shop']
        text = (f"🔍 Подтвердите заказ:\n"
                f"• Точка: {shop['name']}\n"
                f"• Адрес: {shop['address']}\n"
                f"• Файл: {filename}\n\n"
                f"Всё верно?")

        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отменить")]],
            resize_keyboard=True
        )
        confirmation_msg = await message.answer(text, reply_markup=markup)
        await state.update_data(confirmation_msg_id=confirmation_msg.message_id)
        await state.set_state(Form.confirmation)

    except ValueError as ve:
        # Очистка при ошибке
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        if message.chat.id in confirmation_timers:
            confirmation_timers[message.chat.id].cancel()
            del confirmation_timers[message.chat.id]
        await state.clear()
        await message.answer(f"❌ Ошибка: {str(ve)}. Используйте /new_order", reply_markup=types.ReplyKeyboardRemove())
    except Exception as e:
        logging.error(f"Критическая ошибка: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка. Используйте /new_order", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
    finally:
        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass


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
            form_data.add_field('ID_shop', str(user_data['shop']['ID_shop']))
            form_data.add_field('user_id', str(message.chat.id))
            with open(temp_file_path, 'rb') as file:
                form_data.add_field('file', file.read(), filename=user_data['filename'])

            async with session.post(f"{API_URL}/orders", data=form_data) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    user_data = await state.get_data()
                    shop = user_data['shop']
                    await message.answer(
                        f"✅ Заказ №{data['order_id']} принят! Ждем вас на точке {shop['name']} по адресу: {shop['address']}",
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


@dp.message()
async def handle_unknown(message: types.Message):
    await message.reply("Не понимаю тебя, попробуй повторить запрос ☺️")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())