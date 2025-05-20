import os
import logging
import random
import asyncio
import aiohttp
import aiofiles
import json
import websockets
import uuid
import traceback
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ContentType
from PyPDF2 import PdfReader
from io import BytesIO
import pythoncom
import win32com.client

logging.basicConfig(
    level=logging.DEBUG,
    filename='bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

API_TOKEN = '7818669005:AAFyAMagVNx7EfJsK-pVLUBkGLfmMp9J2EQ'
API_URL = 'http://localhost:5000'
UPLOAD_FOLDER = 'C:\\send_to_ptint\\send-to-print\\project\\api\\uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


class Form(StatesGroup):
    shop_selection = State()
    file_processing = State()
    color_selection = State()
    comment = State()
    confirmation = State()


bot = Bot(token=API_TOKEN)
dp = Dispatcher()
timers = {}
confirmation_timers = {}


async def websocket_server():
    # Слушаем на всех интерфейсах и правильном порту
    async with websockets.serve(handler, "0.0.0.0", 8001):
        await asyncio.Future()  # Бесконечное ожидание


async def handler(websocket):
    async for message in websocket:
        try:
            data = json.loads(message)
            if data['type'] == 'status_update':
                user_id = data['user_id']
                order_id = data['order_id']
                address = data['address']

                if data['status'] == 'готов':
                    await bot.send_message(
                        user_id,
                        f"🖨️ Заказ №{order_id} готов! Адрес получения: {address}"
                    )
                elif data['status'] == 'выдан':
                    await bot.send_message(
                        user_id,
                        f"✅ Заказ №{order_id} выдан! Спасибо, что воспользовались нашим сервисом! Ждем вас снова!"
                    )
        except Exception as e:
            logging.error(f"WebSocket Error: {traceback.format_exc()}")


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
            await bot.send_message(chat_id, "❌ Время оформления заказа истекло, ваш заказ отменен", reply_markup=types.ReplyKeyboardRemove())
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
            await bot.send_message(chat_id, "❌ Время подтверждения истекло, ваш заказ отменен", reply_markup=types.ReplyKeyboardRemove())
            await state.clear()
            del confirmation_timers[chat_id]
    except asyncio.CancelledError:
        logging.info("1-минутный таймер отменен")


async def get_page_count(file_path: str, ext: str) -> int:
    try:
        if ext in ('.png', '.jpg', '.jpeg'):
            return 1
        if ext == '.pdf':
            async with aiofiles.open(file_path, 'rb') as f:
                content = await f.read()
                pdf = PdfReader(BytesIO(content))  # Используем BytesIO
                return len(pdf.pages)

        return await asyncio.to_thread(_process_word_file, file_path)

    except Exception as e:
        logging.error(f"Ошибка подсчета страниц: {traceback.format_exc()}")
        raise


def _process_word_file(file_path: str) -> int:
    pythoncom.CoInitialize()
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(os.path.abspath(file_path))
        count = doc.ComputeStatistics(2)
        doc.Close(False)
        return count
    except Exception as e:
        logging.error(f"Word COM Error: {str(e)}")
        raise
    finally:
        word.Quit()
        pythoncom.CoUninitialize()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.", reply_markup=types.ReplyKeyboardRemove()
    )


@dp.message(Command("new_order"))
async def cmd_new_order(message: types.Message, state: FSMContext):
    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]

    if message.chat.id in confirmation_timers:
        confirmation_timers[message.chat.id].cancel()
        del confirmation_timers[message.chat.id]

    user_data = await state.get_data()
    temp_file = user_data.get('temp_file')

    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
            logging.info(f"Удален временный файл: {temp_file}")
        except Exception as e:
            logging.error(f"Ошибка удаления файла: {str(e)}")

    confirmation_msg_id = user_data.get('confirmation_msg_id')
    if confirmation_msg_id:
        try:
            await bot.delete_message(message.chat.id, confirmation_msg_id)
        except Exception as e:
            logging.error(f"Ошибка удаления сообщения: {str(e)}")

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
        f"📍 Адрес: {shop['address']}\n"
        f"💰 Цены:\n"
        f"• Черно-белая: {shop['price_bw']:.2f} руб/стр\n"
        f"• Цветная: {shop['price_cl']:.2f} руб/стр\n\n"
        f"📎 Отправьте PDF, DOC, DOCX файл или PNG, JPEG, JPG картинку размером не более 20 МБ для расчета стоимости\n"
        f"Используйте /reset для отмены заказа."
    )
    await message.answer(response, reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(Form.file_processing)


@dp.message(Form.file_processing, F.content_type == ContentType.DOCUMENT)
async def process_file(message: types.Message, state: FSMContext):
    processing_msg = await message.answer("⏳ Файл обрабатывается, подождите пожалуйста...")
    temp_path = None

    try:
        # 1. Получаем информацию о файле
        file_info = await bot.get_file(message.document.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к файлу")

        # 2. Формируем URL для скачивания
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        logging.info(f"Начинаем загрузку файла: {file_url}")

        # 3. Скачиваем файл (с отключенной проверкой SSL для теста)
        connector = aiohttp.TCPConnector(ssl=False)  # ВНИМАНИЕ: Не для продакшена!
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Ошибка HTTP {resp.status}: {await resp.text()}")

                file_content = await resp.read()
                if not file_content:
                    raise ValueError("Получен пустой файл")

        # 4. Проверяем расширение файла
        filename = message.document.file_name or "unnamed_file"
        file_ext = os.path.splitext(filename)[1].lower()

        if file_ext not in ('.pdf', '.doc', '.docx', '.png', '.jpg', '.jpeg'):
            raise ValueError("Поддерживаются только следующие форматы: PDF, DOC, DOCX, PNG, JPEG, JPG")

        # 5. Сохраняем временный файл
        temp_name = f"temp_{uuid.uuid4()}{file_ext}"
        temp_path = os.path.join(UPLOAD_FOLDER, temp_name)

        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(file_content)

        # 6. Проверяем что файл сохранился
        if not os.path.exists(temp_path):
            raise ValueError("Не удалось сохранить файл на диск")

        # 7. Подсчитываем количество страниц
        pages = await get_page_count(temp_path, file_ext)
        logging.info(f"Определено страниц: {pages}")

        if pages < 1:
            raise ValueError("⚠️ Не удалось определить количество страниц")

        # 8. Сохраняем данные в состояние
        await state.update_data({
            'temp_file': temp_path,
            'pages': pages,
            'file_extension': file_ext[1:],
            'filename': filename,
            'original_file_url': file_url
        })

        # 9. Запрашиваем тип печати
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Черно-белая")],
                [KeyboardButton(text="Цветная")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await message.answer(
            f"📄 Файл успешно обработан!\n"
            f"Количество страниц: {pages}\n"
            f"Выберите тип печати:",
            reply_markup=markup
        )

        await state.set_state(Form.color_selection)

    except ValueError as ve:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        if message.chat.id in confirmation_timers:
            confirmation_timers[message.chat.id].cancel()
            del confirmation_timers[message.chat.id]
        await state.clear()

        error_msg = f"❌ Ошибка: {str(ve)}. Используйте /new_order для начала нового заказа"
        await message.answer(error_msg, reply_markup=types.ReplyKeyboardRemove())
        logging.warning(error_msg)

    except Exception as e:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        if message.chat.id in confirmation_timers:
            confirmation_timers[message.chat.id].cancel()
            del confirmation_timers[message.chat.id]
        await state.clear()

        error_msg = f"❌ Критическая ошибка обработки файла: {str(e)}"
        await message.answer("❌ Произошла непредвиденная ошибка. Используйте /new_order для начала нового заказа", reply_markup=types.ReplyKeyboardRemove())
        logging.error(f"{error_msg}\n{traceback.format_exc()}")
    finally:
        # Очистка в случае ошибки
        if temp_path and os.path.exists(temp_path) and ('temp_file' not in await state.get_data()):
            try:
                os.remove(temp_path)
                logging.info(f"Удален временный файл: {temp_path}")
            except Exception as e:
                logging.error(f"Ошибка удаления временного файла: {str(e)}")

        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception as e:
            logging.error(f"Ошибка удаления сообщения: {str(e)}")


@dp.message(Form.color_selection)
async def process_color(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    color = message.text.lower()
    if color not in ['черно-белая', 'цветная']:
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Черно-белая"), KeyboardButton(text="Цветная")]],
            resize_keyboard=True
        )
        await message.answer("❌ Неверный тип печати! Выберите вариант из кнопок ниже:", reply_markup=markup)
        return

    price = user_data['shop']['price_bw'] if color == 'черно-белая' else user_data['shop']['price_cl']
    total_price = round(price * user_data['pages'], 2)
    await state.update_data(color=color, price=total_price)

    # Добавляем клавиатуру с кнопкой "Без комментария"
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Без комментария")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        "📝 Введите комментарий к заказу или нажмите кнопку ниже:",
        reply_markup=markup
    )
    await state.set_state(Form.comment)


@dp.message(Form.comment)
async def process_comment(message: types.Message, state: FSMContext):
    # Обрабатываем кнопку "Без комментария"
    if message.text == "Без комментария":
        comment = ''
    else:
        comment = message.text

    await state.update_data(comment=comment)
    user_data = await state.get_data()

    # Определяем расширение файла
    file_ext = user_data.get('file_extension', '').lower()

    # Формируем строку стоимости
    cost_line = (
        "• Стоимость: уточняйте на точке"
        if file_ext in ('png', 'jpg', 'jpeg')
        else f"• Стоимость: {user_data['price']:.2f} руб"
    )

    response = (
        f"🔍 Подтвердите заказ:\n"
        f"• Точка: {user_data['shop']['name']} по адресу {user_data['shop']['address']}\n"
        f"• Страниц: {user_data['pages']}\n"
        f"• Тип: {user_data['color']}\n"
        f"{cost_line}\n"  
        f"• Комментарий: {comment if comment else 'нет'}\n"
#        f"Внимание! Это примерные расценки, не являющиеся публичной афертой. Итоговую стоимость уточняйте на точке печати"
    )

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отменить")]],
        resize_keyboard=True
    )

    confirmation_msg = await message.answer(response, reply_markup=markup)

    confirmation_timers[message.chat.id] = asyncio.create_task(
        confirmation_timeout(message.chat.id, state)
    )
    await state.update_data(confirmation_msg_id=confirmation_msg.message_id)
    await state.set_state(Form.confirmation)


@dp.message(Form.confirmation)
async def process_confirmation(message: types.Message, state: FSMContext):
    if message.text not in ["Подтвердить", "Отменить"]:
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Подтвердить"), KeyboardButton(text="Отменить")]],
            resize_keyboard=True
        )
        await message.answer("⚠️ Пожалуйста, используйте кнопки для подтверждения:", reply_markup=markup)
        return

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
                logging.info(f"Удалён временный файл: {temp_file_path}")
            except Exception as e:
                logging.error(f"Ошибка удаления временного файла: {str(e)}")
        await state.clear()
        return

    check_code = random.randint(100000, 999999)

    try:
        async with aiohttp.ClientSession() as session:
            form_data = aiohttp.FormData()
            form_data.add_field('ID_shop', str(user_data['shop']['ID_shop']))
            form_data.add_field('price', str(user_data['price']))
            form_data.add_field('pages', str(user_data['pages']))
            form_data.add_field('color', user_data['color'])
            form_data.add_field('user_id', str(message.chat.id))
            form_data.add_field('note', user_data.get('comment', ''))
            form_data.add_field('file_extension', user_data['file_extension'])
            form_data.add_field('con_code', str(check_code))

            with open(temp_file_path, 'rb') as file:
                form_data.add_field('file', file.read(), filename=user_data['filename'])

            async with session.post(f"{API_URL}/orders", data=form_data) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    await message.answer(
                        f"✅ Заказ №{data['order_id']} принят! Проверочный код: {check_code}",
                        reply_markup=types.ReplyKeyboardRemove()
                    )
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                            logging.info(f"Удалён временный файл: {temp_file_path}")
                        except Exception as e:
                            logging.error(f"Ошибка удаления временного файла: {str(e)}")
                else:
                    await message.answer("❌ Ошибка подтверждения заказа")
    except Exception as e:
        await message.answer("❌ Ошибка создания заказа")
        logging.error(f"Ошибка подтверждения: {traceback.format_exc()}")
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
            try:
                os.remove(temp_file)
                logging.info(f"Удален временный файл: {temp_file}")
            except Exception as e:
                logging.error(f"Ошибка удаления файла: {str(e)}")

        await state.clear()

        confirmation_msg_id = user_data.get('confirmation_msg_id')
        if confirmation_msg_id:
            try:
                await bot.delete_message(message.chat.id, confirmation_msg_id)
            except Exception as e:
                logging.error(f"Ошибка удаления сообщения: {str(e)}")

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
    await asyncio.gather(dp.start_polling(bot), websocket_server())


if __name__ == "__main__":
    asyncio.run(main())