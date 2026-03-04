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

UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


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
                address = data['address']
                check_code = data['con_code']

                if data['status'] == 'ready':
                    await bot.send_message(
                        user_id,
                        f"🖨️ Заказ №{order_id} готов!\n"
                        f"• Адрес получения: {address}.\n"
                        f"• Проверочный код: {check_code}\n"
                        f"Пожалуйста, назовите этот код сотруднику, чтобы забрать заказ."
                    )
                elif data['status'] == 'completed':
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
        logging.error(f"Cleaning error: {str(e)}")


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
async def cmd_start(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке "
        f"документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.",
        reply_markup=types.ReplyKeyboardRemove()
    )


@dp.message(Command("reset"), StateFilter('*'))
async def cmd_reset(message: types.Message, state: FSMContext):
    try:
        if message.chat.id in timers:
            timers[message.chat.id].cancel()
            del timers[message.chat.id]

        user_data = await state.get_data()
        pending_task = user_data.get('pending_task')
        if pending_task and not pending_task.done():
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass
        temp_file = user_data.get('temp_file')

        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                logging.info(f"Temporary file deleted: {temp_file}")
            except Exception as e:
                logging.error(f"Error deleting a temporary file: {str(e)}")

        await state.clear()

        confirmation_msg_id = user_data.get('confirmation_msg_id')
        if confirmation_msg_id:
            try:
                await bot.delete_message(message.chat.id, confirmation_msg_id)
            except Exception as e:
                logging.error(f"Message deletion error: {str(e)}")

        await message.answer(
            "🔄 Все данные сброшены. Вы можете начать новый заказ с помощью /new_order",
            reply_markup=types.ReplyKeyboardRemove()
        )

    except Exception as e:
        logging.error(f"Error in reset: {traceback.format_exc()}")
        await message.answer("❌ Произошла ошибка при сбросе")


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
    if message.chat.id in timers:
        timers[message.chat.id].cancel()
        del timers[message.chat.id]

    user_data = await state.get_data()
    temp_file = user_data.get('temp_file')

    if temp_file and os.path.exists(temp_file):
        try:
            os.remove(temp_file)
            logging.info(f"Temporary file deleted: {temp_file}")
        except Exception as e:
            logging.error(f"Error deleting a temporary file: {str(e)}")

    confirmation_msg_id = user_data.get('confirmation_msg_id')
    if confirmation_msg_id:
        try:
            await bot.delete_message(message.chat.id, confirmation_msg_id)
        except Exception as e:
            logging.error(f"Message deletion error: {str(e)}")

    await state.clear()

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}/shops", headers={"x-api-key": INTERNAL_API_KEY}) as resp:
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
        f"📎 Отправьте PDF, DOC, DOCX файл или PNG, JPEG, JPG картинку размером не более 20 МБ для расчета стоимости\n"
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
        # 1. Получаем информацию о файле
        file_info = await bot.get_file(message.document.file_id)
        if not file_info.file_path:
            raise ValueError("Telegram не вернул путь к файлу")

        # 2. Формируем URL для скачивания
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_info.file_path}"
        logging.info(f"Starting the file download: {file_url}")

        # 3. Скачиваем файл
        connector = aiohttp.TCPConnector(ssl=False)
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
        logging.info(f"Defined pages: {pages}")

        if pages is None or pages < 1:
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
        if temp_path and os.path.exists(temp_path) and ('temp_file' not in await state.get_data()):
            try:
                os.remove(temp_path)
                logging.info(f"Temporary file deleted: {temp_path}")
            except Exception as e:
                logging.error(f"Error deleting a temporary file: {str(e)}")

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
        f"Если все верно - нажмите кнопку '💳 Оплатить'"
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
            try:
                os.remove(temp_file_path)
                logging.info(f"Temporary file deleted: {temp_file_path}")
            except Exception as e:
                logging.error(f"Error deleting a temporary file: {str(e)}")
        await state.clear()
        return

    check_code = random.randint(1000, 9999)

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

            async with session.post(f"{API_URL}/orders", data=form_data, headers={"x-api-key": INTERNAL_API_KEY}) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    order_id = data["order_id"]

                    async with session.post(
                            f"{API_URL}/payments/create",
                            json={"order_id": order_id}
                    ) as payment_resp:

                        if payment_resp.status != 200:
                            await message.answer("❌ Ошибка создания платежа")
                            return

                        payment_data = await payment_resp.json()
                        confirmation_url = payment_data["confirmation_url"]

                    payment_link_message = await message.answer(
                        f"💳 Для завершения заказа перейдите по ссылке для оплаты:\n{confirmation_url}",
                        reply_markup=types.ReplyKeyboardRemove()
                    )
                    link_message_id = payment_link_message.message_id
                    asyncio.create_task(
                        start_payment_polling(order_id, message.chat.id, link_message_id)
                    )

                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                            logging.info(f"Temporary file deleted: {temp_file_path}")
                        except Exception as e:
                            logging.error(f"Error deleting a temporary file: {str(e)}")
                else:
                    await message.answer("❌ Ошибка оплаты заказа")
    except Exception as e:
        await message.answer("❌ Ошибка создания заказа")
        logging.error(f"Confirmation error: {traceback.format_exc()}")
    finally:
        await state.clear()


@dp.message()
async def handle_unknown(message: types.Message):
    await message.reply("Не понимаю тебя, попробуй повторить запрос ☺️")


async def start_payment_polling(order_id: int, chat_id: int, link_message_id: int):
    try:
        # Активная фаза: 3 минуты
        hot_attempts = 36  # 3 минуты (36 * 5 сек)
        for i in range(hot_attempts):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{API_URL}/payments/check/{order_id}", headers={"x-api-key": INTERNAL_API_KEY}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            status = data.get("status")

                            # Сценарий 1: Успешная оплата
                            if status == "paid":
                                check_code = data.get("con_code", "Не найден")
                                await bot.send_message(chat_id, f"✅ Оплата прошла успешно! Заказ №{order_id} принят в работу.\n"
                                                                f"По готовности вам придет уведомление.",
                                                       reply_markup=types.ReplyKeyboardRemove())
                                return  # Успех, полностью выходим

                            # Сценарий 2: Платеж отклонен или отменен пользователем
                            elif status == "canceled":
                                await bot.send_message(
                                    chat_id,
                                    f"❌ Платёж по заказу №{order_id} был отклонён или отменён. "
                                    "Вы можете попробовать оплатить снова, создав новый заказ: /new_order",
                                    reply_markup=types.ReplyKeyboardRemove()
                                )
                                return  # Отмена, полностью выходим

                            # Если статус 'pending', цикл просто продолжится
            except Exception as e:
                logging.error(f"Hot polling for order {order_id} failed: {e}")
                continue
            await asyncio.sleep(5)

        # --- ФАЗА 2: "Теплый" опрос (с 3-й по 12-ю минуту) ---
        # 10 минут = 10 попыток с интервалом 60 сек
        warm_attempts = 8
        for i in range(warm_attempts):
            await asyncio.sleep(60)  # Ждем 1 минуту
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{API_URL}/payments/check/{order_id}") as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            status = data.get("status")

                            if status == "paid":  # Все еще можем поймать успешную оплату
                                await bot.send_message(chat_id,
                                                       f"✅ Оплата прошла успешно! Заказ №{order_id} принят в работу.\n"
                                                       f"По готовности вам придет уведомление.",
                                                       reply_markup=types.ReplyKeyboardRemove())
                                return
                            elif status == "canceled":
                                await bot.send_message(chat_id,
                                                       f"❌ Платёж по заказу №{order_id} был отклонён или отменён.",
                                                       reply_markup=types.ReplyKeyboardRemove())
                                return
            except Exception as e:
                logging.error(f"Polling (warm phase) for order {order_id} failed on attempt {i + 1}: {e}")
                continue

        # --- ФАЗА 3: Финальная отмена ---
        # Этот блок сработает, только если за 12 минут ничего не произошло
        logging.info(f"Finalizing order {order_id} after 12 minutes.")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_URL}/orders/{order_id}/cancel-timeout", headers={"x-api-key": INTERNAL_API_KEY}) as resp:
                if resp.status == 200 and (await resp.json()).get("status") == "canceled":
                    await bot.send_message(
                        chat_id,
                        f"⌛ Время на оплату заказа №{order_id} истекло, и он был автоматически отменен.\n"
                        "Чтобы попробовать снова, создайте новый заказ: /new_order",
                        reply_markup=types.ReplyKeyboardRemove()
                    )
    finally:
        try:
            await bot.delete_message(chat_id, link_message_id)
            logging.info(f"Payment link message {link_message_id} deleted for chat {chat_id}.")
        except Exception:
            pass


async def main():
    await asyncio.gather(dp.start_polling(bot), websocket_server())


if __name__ == "__main__":
    asyncio.run(main())