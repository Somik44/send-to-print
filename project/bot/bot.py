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
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ContentType
from PyPDF2 import PdfReader
from io import BytesIO
from aiohttp import web
import pythoncom
import win32com.client
import tempfile
import zipfile
import xml.dom.minidom
import docx
from dotenv import load_dotenv
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.DEBUG,
    filename='bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL")
LIBREOFFICE_PATH = os.getenv("LIBREOFFICE_PATH")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()]
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
MAX_FILE_SIZE = 20 * 1024 * 1024

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

active_orders = {}
payment_messages = {}


def is_admin(message: types.Message):
    return message.from_user.id in ADMIN_IDS


class Form(StatesGroup):
    shop_selection = State()
    file_processing = State()
    color_selection = State()
    comment = State()
    confirmation = State()
    admin_broadcast = State()
    confirm_broadcast = State()


bot = Bot(token=API_TOKEN)
dp = Dispatcher()
timers = {}


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
                status = data['status']

                if status in ('paid', 'expired', 'canceled', 'ready', 'completed'):
                    await delete_payment_message(user_id)
                    if user_id in active_orders and active_orders[user_id] == order_id:
                        del active_orders[user_id]
                        logging.info(f"Removed order {order_id} from active_orders for user {user_id}")

                if status == 'paid':
                    await bot.send_message(
                        user_id,
                        f"✅ Оплата прошла успешно! Заказ №{order_id} принят в работу.\n"
                        "По готовности вам придет уведомление."
                    )
                elif status == 'ready':
                    address = data['address']
                    check_code = data['con_code']
                    await bot.send_message(
                        user_id,
                        f"🖨️ Заказ №{order_id} готов!\n"
                        f"• Адрес получения: {address}.\n"
                        f"• Проверочный код: {check_code}\n"
                        f"Пожалуйста, назовите этот код сотруднику, чтобы забрать заказ."
                    )
                elif status == 'completed':
                    await bot.send_message(
                        user_id,
                        f"✅ Заказ №{order_id} выдан! Спасибо, что воспользовались нашим сервисом! Ждем вас снова!"
                    )
                elif status == 'expired':
                    await bot.send_message(
                        user_id,
                        f"⌛ Время оплаты заказа №{order_id} истекло. Заказ отменён.\n"
                        "Вы можете создать новый заказ: /new_order"
                    )
                elif status == 'canceled':
                    await bot.send_message(
                        user_id,
                        f"❌ Платёж по заказу №{order_id} был отклонён или отменён.\n"
                        "Вы можете попробовать оплатить снова, создав новый заказ: /new_order"
                    )
        except Exception as e:
            logging.error(f"WebSocket Error: {traceback.format_exc()}")


async def cleanup_order_data(user_data: dict):
    try:
        if 'order_id' in user_data:
            async with aiohttp.ClientSession() as session:
                await session.delete(f"{API_URL}/orders/{user_data['order_id']}")
    except Exception as e:
        logging.error(f"Cleaning error: {str(e)}")


async def cancel_order_via_api(order_id: int):
    """Отменяет заказ через защищённый эндпоинт API."""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-api-Key": INTERNAL_API_KEY}  # используем INTERNAL_API_KEY
            async with session.post(f"{API_URL}/admin/orders/{order_id}/cancel", headers=headers) as resp:
                if resp.status == 200:
                    logging.info(f"Order {order_id} cancelled via API")
                else:
                    logging.error(f"Failed to cancel order {order_id}, status: {resp.status}")
    except Exception as e:
        logging.error(f"Error cancelling order {order_id}: {e}")


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
        logging.info("The 10-minute timer has been canceled")


async def start_new_order_process(message: types.Message, state: FSMContext):
    """Запускает процесс выбора магазина для нового заказа."""
    user_id = message.chat.id

    await delete_payment_message(user_id)

    # Отменяем старый таймер, если есть
    if user_id in timers:
        timers[user_id].cancel()
        del timers[user_id]

    # Очищаем временные файлы из предыдущего состояния
    user_data = await state.get_data()
    temp_file = user_data.get('temp_file')
    if temp_file and os.path.exists(temp_file):
        os.remove(temp_file)

    # Получаем список магазинов
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/shops", headers={"x-api-key": INTERNAL_API_KEY}) as resp:
            if resp.status == 429:
                await message.answer("❗ Не спамьте!")
                return
            elif resp.status != 200:
                await message.answer("❌ Ошибка загрузки магазинов")
                return
            shops = await resp.json()

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=shop['name'])] for shop in shops],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("🏪 Выберите точку печати из списка:", reply_markup=markup)

    # Запускаем таймер
    timers[user_id] = asyncio.create_task(start_order_timer(user_id, state))
    await state.set_state(Form.shop_selection)


async def handle_cancel_and_start(callback: types.CallbackQuery, state: FSMContext, after_cancel: str):
    """
    after_cancel может быть:
    - 'welcome'  → после отмены отправить приветственное сообщение
    - 'new_order' → после отмены сразу начать новый заказ (выбор точки)
    """
    user_id = callback.from_user.id
    order_id = active_orders.get(user_id)

    if order_id:
        await cancel_order_via_api(order_id)
        await callback.message.answer(
            f"❌ Платёж по заказу №{order_id} был отклонён или отменён."
        )
        del active_orders[user_id]
    else:
        await callback.message.answer("Активный заказ не найден.")

    await state.clear()

    if after_cancel == 'welcome':
        await callback.message.answer(
            f"Привет, {callback.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке "
            f"документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    elif after_cancel == 'new_order':
        await start_new_order_process(callback.message, state)

    await callback.answer()


async def is_order_active(order_id: int) -> bool:
    """Проверяет, находится ли заказ в статусе waiting_payment (активен)"""
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


async def delete_payment_message(user_id: int):
    """Удаляет сообщение с платёжной ссылкой, если оно есть в словаре"""
    user_id = int(user_id)
    if user_id in payment_messages:
        try:
            await bot.delete_message(user_id, payment_messages[user_id])
            logging.info(f"Deleted payment message for user {user_id}")
            del payment_messages[user_id]
        except Exception as e:
            logging.error(f"Failed to delete payment message for user {user_id}: {e}")
    else:
        # Полезно для отладки – выводим текущее состояние словаря
        logging.warning(f"No payment message found for user {user_id}, current_dict={payment_messages}")


async def get_page_count(file_path: str, ext: str) -> int:
    try:
        if ext in ('.png', '.jpg', '.jpeg'):
            return 1
        if ext == '.pdf':
            async with aiofiles.open(file_path, 'rb') as f:
                content = await f.read()
                pdf = PdfReader(BytesIO(content))  # Используем BytesIO
                return len(pdf.pages)

        # return await asyncio.to_thread(_process_word_file, file_path)
        return await get_word_page_count_via_libreoffice(file_path)

    except Exception as e:
        logging.error(f"Page count error: {traceback.format_exc()}")
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


async def get_pdf_page_count(file_path: str) -> int:
    """Подсчет страниц в PDF файле"""
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            content = await f.read()
            pdf = PdfReader(BytesIO(content))
            return len(pdf.pages)
    except Exception as e:
        logging.error(f"PDF page count error: {str(e)}")


async def get_word_page_count_via_libreoffice(file_path: str) -> int:
    """
    Точный подсчет страниц Word документов через LibreOffice для Windows.
    """
    temp_dir = None

    try:
        # 1. Создаем временную директорию
        temp_dir = tempfile.mkdtemp()

        base_name = os.path.basename(file_path)
        file_name_without_ext = os.path.splitext(base_name)[0]
        pdf_output_path = os.path.join(temp_dir, f"{file_name_without_ext}.pdf")

        # 2. Запускаем конвертацию
        cmd = [
            LIBREOFFICE_PATH, '--headless', '--convert-to', 'pdf',
            '--outdir', temp_dir, file_path
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        # 3. Проверка результата конвертации
        if process.returncode != 0:
            logging.error(f"LibreOffice conversion failed: {stderr.decode()}")
            return await get_docx_page_count_metadata(file_path) if file_path.endswith('.docx') else 0

        if not os.path.exists(pdf_output_path):
            logging.error(f"PDF file was not created. Expected path: {pdf_output_path}")
            return await get_docx_page_count_metadata(file_path) if file_path.endswith('.docx') else 0
        logging.info("LibreOffice conversation successfully")
        # 4. Подсчет страниц
        page_count = await get_pdf_page_count(pdf_output_path)

        # 5. Очистка
        try:
            if os.path.exists(pdf_output_path):
                os.remove(pdf_output_path)
            if temp_dir and os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except Exception as e:
            logging.warning(f"Cleanup error: {e}")

        return page_count or 0

    except Exception as e:
        logging.error(f"LibreOffice critical error: {traceback.format_exc()}")

        # Финальная очистка при крахе
        if temp_dir and os.path.exists(temp_dir):
            try:
                for f in os.listdir(temp_dir):
                    os.remove(os.path.join(temp_dir, f))
                os.rmdir(temp_dir)
            except:
                pass

        if file_path.lower().endswith('.docx'):
            try:
                return await get_docx_page_count_metadata(file_path)
            except:
                return 0
        return 0


# Версия подсчета страниц через либреофис для Linux
# async def get_word_page_count_via_libreoffice(file_path: str) -> int:
#     """
#     Точный подсчет страниц Word документов через LibreOffice (версия для Linux)
#     """
#     # В Linux команда обычно доступна просто как 'libreoffice' или 'soffice'
#     libreoffice_bin = "libreoffice"
#     temp_dir = None
#
#     try:
#         temp_dir = tempfile.mkdtemp()
#
#         # В Linux LibreOffice создает PDF с тем же именем, что и оригинал
#         base_name = os.path.basename(file_path)
#         file_name_without_ext = os.path.splitext(base_name)[0]
#         pdf_output_path = os.path.join(temp_dir, f"{file_name_without_ext}.pdf")
#
#         # Команда для Linux.
#         # Добавляем параметр -env для изоляции профиля пользователя (нужно для стабильности на сервере)
#         cmd = [
#             libreoffice_bin,
#             '--headless',
#             f'-env:UserInstallation=file://{temp_dir}/profile',
#             '--convert-to', 'pdf',
#             '--outdir', temp_dir,
#             file_path
#         ]
#
#         process = await asyncio.create_subprocess_exec(
#             *cmd,
#             stdout=asyncio.subprocess.PIPE,
#             stderr=asyncio.subprocess.PIPE
#         )
#
#         stdout, stderr = await process.communicate()
#
#         if process.returncode != 0:
#             logging.error(f"LibreOffice failed: {stderr.decode()}")
#             # Если не сработало, пробуем метод через метаданные (для .docx)
#             if file_path.lower().endswith('.docx'):
#                 return await get_docx_page_count_metadata(file_path)
#             return 0
#
#         if not os.path.exists(pdf_output_path):
#             logging.error(f"PDF not found at {pdf_output_path}")
#             return 0
#
#         page_count = await get_pdf_page_count(pdf_output_path)
#
#         # Очистка
#         try:
#             import shutil
#             shutil.rmtree(temp_dir)
#         except:
#             pass
#
#         return page_count or 0
#
#     except Exception as e:
#         logging.error(f"Linux LibreOffice error: {str(e)}")
#         if file_path.lower().endswith('.docx'):
#             try:
#                 return await get_docx_page_count_metadata(file_path)
#             except:
#                 pass
#         return 0


async def get_docx_page_count_metadata(file_path: str) -> int:
    """
    Подсчет страниц через метаданные DOCX (быстрый, достаточно точный, но не умеет работать с .doc и файлами без metadata)
    """
    try:
        with zipfile.ZipFile(file_path, 'r') as document:
            dxml = document.read('docProps/app.xml')
            uglyXml = xml.dom.minidom.parseString(dxml)
            page_element = uglyXml.getElementsByTagName('Pages')[0]
            page_count = int(page_element.childNodes[0].nodeValue)
            return page_count
    except Exception as e:
        logging.error(f"DOCX metadata page count error: {str(e)}")


# async def get_doc_page_count_fallback(file_path: str) -> int:
#      """
#      Fallback для .doc файлов через antiword
#      """
#      try:
#          # Проверяем доступность antiword
#          result = subprocess.run(['which', 'antiword'], capture_output=True, text=True)
#          if result.returncode != 0:
#              logging.warning("antiword not found, using file size estimation")
#              return await get_doc_page_count_by_size(file_path)
#
#          # Используем antiword для подсчета страниц
#          cmd = ['antiword', file_path]
#          process = await asyncio.create_subprocess_exec(
#              *cmd,
#              stdout=asyncio.subprocess.PIPE,
#              stderr=asyncio.subprocess.PIPE
#          )
#
#          stdout, stderr = await process.communicate()
#
#          if process.returncode == 0:
#              text = stdout.decode('utf-8', errors='ignore')
#              # Подсчет страниц по количеству символов (приблизительно)
#              # В среднем 1800-2000 символов на страницу
#              char_count = len(text)
#              page_count = max(1, char_count // 1800)
#              return page_count
#          else:
#              logging.error(f"antiword failed: {stderr.decode()}")
#      except Exception as e:
#          logging.error(f"antiword page count error: {str(e)}")


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.chat.id

    # Проверяем наличие активного заказа в памяти
    if user_id in active_orders:
        order_id = active_orders[user_id]
        # Проверяем реальный статус заказа в БД
        if not await is_order_active(order_id):
            # Заказ уже не активен – удаляем из памяти и показываем приветствие
            del active_orders[user_id]
            await message.answer(
                f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке "
                f"документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.",
                reply_markup=types.ReplyKeyboardRemove()
            )
            return

        # Заказ действительно активен – показываем только диалог отмены
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Отменить и начать заново", callback_data="cancel_and_start_from_start")
        builder.button(text="❌ Продолжить текущий", callback_data="continue_current")
        await message.answer(
            "У вас есть незавершённый заказ (ожидает оплаты). Что хотите сделать?",
            reply_markup=builder.as_markup()
        )
        return

    # Если активного заказа нет, показываем приветствие
    await message.answer(
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке "
        f"документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.",
        reply_markup=types.ReplyKeyboardRemove()
    )


@dp.message(Command("reset"), StateFilter('*'))
async def cmd_reset(message: types.Message, state: FSMContext):
    user_id = message.chat.id
    user_data = await state.get_data()

    try:
        await delete_payment_message(user_id)

        # Отменяем таймер, если есть
        if user_id in timers:
            timers[user_id].cancel()
            del timers[user_id]

        # Отменяем фоновую задачу, если есть
        pending_task = user_data.get('pending_task')
        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass

        # Если есть активный заказ, отменяем его через API
        if user_id in active_orders:
            order_id = active_orders[user_id]
            await cancel_order_via_api(order_id)
            if order_id:
                await message.answer(
                    f"❌ Платёж по заказу №{order_id} был отклонён или отменён.\n"
                )
            del active_orders[user_id]

        # Удаляем временный файл
        temp_file = user_data.get('temp_file')
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

        # Очищаем состояние
        await state.clear()

        await message.answer(
            "🔄 Все данные сброшены. Вы можете начать новый заказ с помощью /new_order",
            reply_markup=types.ReplyKeyboardRemove()
        )

    except Exception as e:
        logging.error(f"Error in reset: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка при сбросе")


@dp.callback_query(F.data == "cancel_and_start_from_start")
async def cancel_and_start_from_start(callback: types.CallbackQuery, state: FSMContext):
    await delete_payment_message(callback.from_user.id)
    await callback.message.delete()

    user_id = callback.from_user.id
    order_id = active_orders.get(user_id)

    if order_id:
        await cancel_order_via_api(order_id)

        # Отправляем уведомление об отмене
        await callback.message.answer(
            f"❌ Платёж по заказу №{order_id} был отклонён или отменён.\n"
        )

        del active_orders[user_id]

        # Отправляем приветствие
        await callback.message.answer(
            f"Привет, {callback.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке "
            f"документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await callback.message.answer("Активный заказ не найден.")

    await state.clear()
    await callback.answer()


@dp.callback_query(F.data == "cancel_and_start_from_new_order")
async def cancel_and_start_from_new_order(callback: types.CallbackQuery, state: FSMContext):
    """Отмена заказа из команды /new_order с автоматическим переходом к выбору точки"""
    await delete_payment_message(callback.from_user.id)
    await callback.message.delete()
    user_id = callback.from_user.id
    order_id = active_orders.get(user_id)

    if order_id:
        await cancel_order_via_api(order_id)
        await callback.message.answer(
            f"❌ Платёж по заказу №{order_id} был отклонён или отменён."
        )
        del active_orders[user_id]
    else:
        await callback.message.answer("Активный заказ не найден.")

    await state.clear()
    await start_new_order_process(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "reset_and_new")
async def reset_and_new(callback: types.CallbackQuery, state: FSMContext):
    # Просто сбрасываем состояние и начинаем новый
    await callback.message.delete()
    await state.clear()
    await start_new_order_process(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "continue_current")
async def continue_current(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    user_id = callback.from_user.id
    if user_id in active_orders:
        await callback.message.answer(
            "Пожалуйста, завершите оплату текущего заказа. Если возникли проблемы, используйте /reset для отмены."
        )
    else:
        # Если нет активного заказа, но состояние есть, просто напоминаем
        await callback.message.answer("Продолжайте оформление заказа.")
    await callback.answer()


@dp.message(Command("broadcast"), is_admin)
async def start_broadcast(message: types.Message, state: FSMContext):
    await state.set_state(Form.admin_broadcast)
    await message.answer("📝 Пришлите сообщение для рассылки (текст/фото).\nДля отмены: /reset")


@dp.message(Form.admin_broadcast, is_admin)
async def preview_broadcast(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("broadcast_photos", [])
    caption = data.get("broadcast_caption", "")
    pending_task = data.get("pending_task")
    current_group_id = data.get("media_group_id")

    # Если пришло фото
    if message.photo:
        file_id = message.photo[-1].file_id
        group_id = message.media_group_id

        # Берём подпись, если есть
        if message.caption:
            caption = message.caption

        # Одиночное фото (без group_id) – сразу показываем превью
        if group_id is None:
            # Отменяем предыдущую задачу, если была
            if pending_task and not pending_task.done():
                pending_task.cancel()
                try:
                    await pending_task
                except asyncio.CancelledError:
                    pass
            photos = [file_id]
            await state.update_data(
                broadcast_photos=photos,
                broadcast_caption=caption,
                pending_task=None,
                media_group_id=None
            )
            await show_broadcast_preview(message, state, photos, caption)
            return

        # Это альбом – добавляем фото
        photos.append(file_id)
        await state.update_data(
            broadcast_photos=photos,
            broadcast_caption=caption,
            media_group_id=group_id
        )

        # Если задача уже существует для этой же группы – просто ждём
        if pending_task and not pending_task.done() and current_group_id == group_id:
            return

        # Если есть задача для другой группы – отменяем её
        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass

        # Запускаем новую задачу ожидания завершения альбома
        async def delayed_show():
            await asyncio.sleep(1.0)  # даём время собрать все фото
            current_data = await state.get_data()
            if current_data.get("media_group_id") == group_id:
                await show_broadcast_preview(
                    message, state,
                    current_data.get("broadcast_photos", []),
                    current_data.get("broadcast_caption", "")
                )

        task = asyncio.create_task(delayed_show())
        await state.update_data(pending_task=task, media_group_id=group_id)

    # Если пришёл текст (не фото)
    elif message.text:
        # Отменяем предыдущую задачу, если была
        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
        await state.update_data(
            broadcast_caption=message.text,
            broadcast_photos=[],
            pending_task=None,
            media_group_id=None
        )
        await show_broadcast_preview(message, state, [], message.text)


async def show_broadcast_preview(message: types.Message, state: FSMContext, photos: list, caption: str):
    """Показывает предпросмотр рассылки и переводит в состояние подтверждения"""
    # Очищаем временные данные, связанные с альбомом
    await state.update_data(pending_task=None, media_group_id=None)
    await state.set_state(Form.confirm_broadcast)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить и отправить", callback_data="send_now")
    builder.button(text="❌ Отмена", callback_data="reset_broadcast")

    await message.answer("👇 Превью рассылки:")

    if photos:
        album = MediaGroupBuilder(caption=caption)
        for p_id in photos:
            album.add_photo(media=p_id)
        await bot.send_media_group(chat_id=message.chat.id, media=album.build())
    else:
        await message.answer(caption)

    await message.answer("Отправить это сообщение всем пользователям?", reply_markup=builder.as_markup())


@dp.callback_query(Form.confirm_broadcast, F.data == "send_now")
async def final_send_broadcast(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending_task = data.get('pending_task')
    if pending_task and not pending_task.done():
        pending_task.cancel()
        try:
            await pending_task
        except asyncio.CancelledError:
            pass
    photos = data.get("broadcast_photos", [])
    caption = data.get("broadcast_caption", "")
    await state.clear()

    await callback.message.edit_text("⏳ Получаю список пользователей...")
    headers = {"X-API-Key": INTERNAL_API_KEY}

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/users/all-ids", headers=headers) as resp:
            if resp.status != 200:
                await callback.message.answer("❌ Ошибка API.")
                return
            user_ids = await resp.json()

    await callback.message.answer(f"🚀 Рассылка на {len(user_ids)} чел...")
    sent, blocked = 0, 0

    for uid in user_ids:
        try:
            if photos:
                # ВОТ ЗДЕСЬ ОТПРАВКА ОДНИМ СООБЩЕНИЕМ (АЛЬБОМОМ)
                album = MediaGroupBuilder(caption=caption)
                for p_id in photos[:10]:  # Лимит ТГ - 10 фото
                    album.add_photo(media=p_id)
                await bot.send_media_group(chat_id=uid, media=album.build())
            else:
                await bot.send_message(chat_id=uid, text=caption)

            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            blocked += 1

    await callback.message.answer(f"📊 Итог:\n✅ Успешно: {sent}\n🚫 Заблокировали: {blocked}")


@dp.callback_query(Form.confirm_broadcast, F.data == "reset_broadcast")
async def cancel_broadcast(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending_task = data.get('pending_task')
    if pending_task and not pending_task.done():
        pending_task.cancel()
        try:
            await pending_task
        except asyncio.CancelledError:
            pass
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()


@dp.message(Command("new_order"))
async def cmd_new_order(message: types.Message, state: FSMContext):
    user_id = message.chat.id

    # Проверяем наличие активного заказа в памяти
    if user_id in active_orders:
        order_id = active_orders[user_id]
        # Проверяем реальный статус заказа в БД
        if not await is_order_active(order_id):
            # Заказ уже не активен – удаляем из памяти и начинаем новый
            del active_orders[user_id]
            await start_new_order_process(message, state)
            return

        # Заказ действительно активен – предлагаем отменить или продолжить
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Отменить", callback_data="cancel_and_start_from_new_order")
        builder.button(text="❌ Продолжить", callback_data="continue_current")
        await message.answer(
            "У вас есть незавершённый заказ (ожидает оплаты). Что хотите сделать?",
            reply_markup=builder.as_markup()
        )
        return

    await start_new_order_process(message, state)


@dp.message(Form.shop_selection)
async def process_shop(message: types.Message, state: FSMContext):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/shops/{message.text}", headers={"x-api-key": INTERNAL_API_KEY}) as resp:
            if resp.status != 200:
                await message.answer("❌ Точка не найдена. /new_order")
                return
            shop = await resp.json()

    await state.update_data(shop=shop)
    response = (
        f"🏪 Выбрана точка: {shop['name']}\n"
        f"⌚ Время работы: {shop['w_hours']}\n"
        f"📍 Адрес: {shop['address']}\n"
        f"💰 Цены:\n"
        f"• Черно-белая: {shop['price_bw']:.2f} руб/стр\n"
        f"• Цветная: {shop['price_cl']:.2f} руб/стр\n\n"
        f"📎 Отправьте один PDF, DOC, DOCX, PNG, JPEG, JPG файл или одну фотографию размером не более 20 МБ для расчета стоимости\n"
        f"Используйте /reset для отмены заказа."
    )
    await message.answer(response, reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(Form.file_processing)


@dp.message(Form.file_processing, F.content_type == ContentType.DOCUMENT)
async def process_file(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    if user_data.get('temp_file'):
        await message.answer(
            "❌ Вы уже отправили файл. Дождитесь обработки или отмените текущий заказ командой /reset.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    # Проверка на альбом (медиа-группу) – несколько файлов в одном сообщении
    if message.media_group_id:
        await message.answer(
            "❌ Пожалуйста, отправляйте файлы по одному. Сначала дождитесь обработки текущего файла.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    processing_msg = await message.answer("⏳ Файл обрабатывается, подождите пожалуйста...")
    temp_path = None

    try:
        # 1. Получаем информацию о файле
        file_info = await bot.get_file(message.document.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к файлу")

        # 2. Формируем URL для скачивания
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        logging.info(f"Starting the file download: {file_url}")

        # 3. Скачиваем файл
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Ошибка HTTP {resp.status}: {await resp.text()}")

                file_content = await resp.read()
                if not file_content:
                    raise ValueError("Получен пустой файл")

                if len(file_content) > MAX_FILE_SIZE:
                    raise ValueError("Файл слишком большой. Максимальный размер — 20 МБ.")

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
        logging.info(f"Defined pages: {pages}")

        if pages is None or pages < 1:
            raise ValueError("⚠️ Не удалось определить количество страниц")

        if pages > 500:
            raise ValueError("Слишком много страниц")

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
        await state.clear()

        error_msg = f"❌ Ошибка: {str(ve)}. Используйте /new_order для начала нового заказа"
        await message.answer(error_msg, reply_markup=types.ReplyKeyboardRemove())
        logging.warning(error_msg)

    except Exception as e:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        await state.clear()

        error_msg = f"❌ Критическая ошибка обработки файла: {str(e)}"
        await message.answer("❌ Произошла непредвиденная ошибка. Используйте /new_order для начала нового заказа", reply_markup=types.ReplyKeyboardRemove())
        logging.error(f"{error_msg}\n{traceback.format_exc()}")
    finally:
        # Очистка в случае ошибки
        state_data = await state.get_data()
        if temp_path and os.path.exists(temp_path) and state_data.get('temp_file') != temp_path:
            try:
                os.remove(temp_path)
                logging.info(f"Temporary file deleted: {temp_path}")
            except Exception as e:
                logging.error(f"Error deleting a temporary file: {str(e)}")

        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception as e:
            logging.error(f"Message deletion error: {str(e)}")


@dp.message(Form.file_processing, F.content_type == ContentType.PHOTO)
async def process_photo(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    if user_data.get('temp_file'):
        await message.answer(
            "❌ Вы уже отправили файл. Дождитесь обработки или отмените текущий заказ командой /reset.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    # Проверка на альбом (медиа-группу) – несколько фото в одном сообщении
    if message.media_group_id:
        await message.answer(
            "❌ Пожалуйста, отправляйте фото по одному. Сначала дождитесь обработки текущего файла.",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    processing_msg = await message.answer("⏳ Фото обрабатывается, подождите пожалуйста...")
    temp_path = None

    try:
        # 1. Берём самое большое фото (последний элемент в массиве)
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к фото")

        # 2. Формируем URL для скачивания
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        logging.info(f"Starting the photo download: {file_url}")

        # 3. Скачиваем фото
        connector = aiohttp.TCPConnector(ssl=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Ошибка HTTP {resp.status}: {await resp.text()}")
                file_content = await resp.read()
                if not file_content:
                    raise ValueError("Получен пустой файл")

        # 4. Определяем расширение (Telegram всегда сохраняет фото в JPEG)
        file_ext = '.jpg'
        filename = f"photo_{message.from_user.id}_{uuid.uuid4()}.jpg"

        # 5. Сохраняем временный файл
        temp_name = f"temp_{uuid.uuid4()}{file_ext}"
        temp_path = os.path.join(UPLOAD_FOLDER, temp_name)
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(file_content)

        if not os.path.exists(temp_path):
            raise ValueError("Не удалось сохранить фото на диск")

        # 6. Подсчитываем страницы (для фото всегда 1)
        pages = 1
        logging.info(f"Defined pages: {pages}")

        # 7. Сохраняем данные в состояние
        await state.update_data({
            'temp_file': temp_path,
            'pages': pages,
            'file_extension': 'jpg',  # или 'jpeg'
            'filename': filename,
            'original_file_url': file_url
        })

        # 8. Запрашиваем тип печати
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Черно-белая")],
                [KeyboardButton(text="Цветная")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await message.answer(
            f"📸 Фото успешно обработано!\n"
            f"Количество страниц: {pages}\n"
            f"Выберите тип печати:",
            reply_markup=markup
        )
        await state.set_state(Form.color_selection)

    except ValueError as ve:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        await state.clear()
        await message.answer(f"❌ Ошибка: {str(ve)}. Используйте /new_order для начала нового заказа", reply_markup=types.ReplyKeyboardRemove())
        logging.warning(str(ve))

    except Exception as e:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]
        await state.clear()
        logging.error(f"Photo processing error: {traceback.format_exc()}")
        await message.answer("❌ Произошла непредвиденная ошибка. Используйте /new_order для начала нового заказа", reply_markup=types.ReplyKeyboardRemove())
    finally:
        if temp_path and os.path.exists(temp_path) and 'temp_file' not in await state.get_data():
            try:
                os.remove(temp_path)
            except Exception as e:
                logging.error(f"Error deleting temp file: {e}")
        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception as e:
            logging.error(f"Message deletion error: {str(e)}")


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


@dp.message(Form.comment, ~F.text.startswith("/"))
async def process_comment(message: types.Message, state: FSMContext):
    # Обрабатываем кнопку "Без комментария"
    if message.text == "Без комментария":
        comment = ''
    else:
        comment = message.text

    # Проверяем длину комментария
    if len(comment) > 255:
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Без комментария")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.answer(
            "❌ Комментарий слишком длинный! Максимальная длина - 255 символов.\n"
            "📝 Введите комментарий к заказу или нажмите кнопку ниже:",
            reply_markup=markup
        )
        return  # Остаемся в состоянии Form.comment

    await state.update_data(comment=comment)
    user_data = await state.get_data()

    response = (
        f"🔍 Подтвердите заказ:\n"
        f"• Точка: {user_data['shop']['name']} по адресу {user_data['shop']['address']}\n"
        f"• Страниц: {user_data['pages']}\n"
        f"• Тип: {user_data['color']}\n"
        f"• Стоимость: {user_data['price']:.2f} руб\n"  
        f"• Комментарий: {comment if comment else 'нет'}\n"
        f"Если все верно - нажмите кнопку:\n'💳 Оплатить'"
    )

    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="💳 Оплатить"), KeyboardButton(text="Отменить")]],
        resize_keyboard=True
    )

    confirmation_msg = await message.answer(response, reply_markup=markup)

    await state.update_data(confirmation_msg_id=confirmation_msg.message_id)
    await state.set_state(Form.confirmation)


@dp.message(Form.confirmation)
async def process_confirmation(message: types.Message, state: FSMContext):
    if message.text not in ["💳 Оплатить", "Отменить"]:
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="💳 Оплатить"), KeyboardButton(text="Отменить")]],
            resize_keyboard=True
        )
        await message.answer("⚠️ Пожалуйста, используйте кнопки для оплаты:", reply_markup=markup)
        return

    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]

    user_data = await state.get_data()
    temp_file_path = user_data.get('temp_file')

    if message.text == 'Отменить':
        await message.answer("❌ Заказ отменен", reply_markup=types.ReplyKeyboardRemove())
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        await state.clear()
        return

    check_code = random.randint(1000, 9999)
    processing_msg = await message.answer("⏳ Ссылка на оплату формируется, подождите пожалуйста...")

    order_id = None
    sent_message = None

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Создаём заказ в API
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

            async with session.post(f"{API_URL}/orders", data=form_data,
                                    headers={"x-api-key": INTERNAL_API_KEY}) as resp:
                if resp.status != 201:
                    error_text = await resp.text()
                    logging.error(f"Order creation failed: {resp.status}, {error_text}")
                    await message.answer("❌ Ошибка создания заказа", reply_markup=types.ReplyKeyboardRemove())
                    return
                order_data = await resp.json()
                order_id = order_data["order_id"]
                logging.info(f"Order {order_id} created successfully")

            # 2. Создаём платёж
            async with session.post(
                    f"{API_URL}/payments/create",
                    json={"order_id": order_id}
            ) as payment_resp:
                if payment_resp.status != 200:
                    error_text = await payment_resp.text()
                    logging.error(f"Payment creation failed: {payment_resp.status}, {error_text}")

                    if order_id:
                        await cancel_order_via_api(order_id)

                    await message.answer("❌ Ошибка создания платежа", reply_markup=types.ReplyKeyboardRemove())
                    return

                payment_info = await payment_resp.json()
                confirmation_url = payment_info.get("confirmation_url")

                if not confirmation_url:
                    logging.error(f"Payment response missing confirmation_url: {payment_info}")
                    if order_id:
                        await cancel_order_via_api(order_id)
                    await message.answer("❌ Неверный ответ от платёжного шлюза",
                                         reply_markup=types.ReplyKeyboardRemove())
                    return

        # 3. Всё хорошо – отправляем ссылку
        sent_message = await message.answer(
            f"💳 Для завершения заказа перейдите по ссылке:\n{confirmation_url}",
            reply_markup=types.ReplyKeyboardRemove()
        )

        # 4. СОХРАНЯЕМ ВСЁ НЕОБХОДИМОЕ
        if sent_message:
            # Сохраняем в словарь для удаления сообщения
            payment_messages[message.chat.id] = sent_message.message_id
            logging.info(f"Saved payment message {sent_message.message_id} for user {message.chat.id}")

            # Сохраняем в active_orders
            active_orders[message.chat.id] = order_id
            logging.info(f"Order {order_id} saved to active_orders for user {message.chat.id}")

            # Сохраняем в состоянии (для /reset)
            await state.update_data(
                order_id=order_id,
                payment_message_id=sent_message.message_id
            )

    except Exception as e:
        logging.error(f"Payment creation error: {traceback.format_exc()}")

        if order_id:
            await cancel_order_via_api(order_id)

        await message.answer(
            "❌ Произошла ошибка при создании заказа/платежа. Попробуйте позже.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    finally:
        # Удаляем сообщение "обработка..."
        try:
            await bot.delete_message(message.chat.id, processing_msg.message_id)
        except Exception as e:
            logging.error(f"Message deletion error: {str(e)}")

        # Удаляем временный файл
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

        # Очищаем состояние ТОЛЬКО если была ошибка
        if not order_id:
            await state.clear()


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
    await asyncio.gather(dp.start_polling(bot), websocket_server())


if __name__ == "__main__":
    asyncio.run(main())