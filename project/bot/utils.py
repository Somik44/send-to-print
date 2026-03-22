import os
import aiofiles
import magic
import zipfile
import logging
import asyncio
import pyclamd
import tempfile
import xml.dom.minidom
from io import BytesIO
from PyPDF2 import PdfReader
import traceback
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

# Константы (можно загружать из .env, но для простоты оставим здесь)
MAX_FILE_SIZE = 20 * 1024 * 1024          # 20 МБ
MAX_ZIP_ENTRIES = 100                     # Не более 100 файлов в архиве
MAX_UNCOMPRESSED_SIZE = 100 * 1024 * 1024 # 100 МБ лимит на распаковку
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER")

# ClamAV (опционально)
CLAMAV_HOST = os.getenv("CLAMAV_HOST")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT"))

# Создаём папку для загрузок, если её нет
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ----------------------------------------------------------------------
# Скачивание файла по URL (асинхронно)
# ----------------------------------------------------------------------
async def download_file_from_url(url: str) -> bytes:
    """Скачивает файл по URL и возвращает его содержимое."""
    import aiohttp
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Ошибка HTTP {resp.status}")
            return await resp.read()


# ----------------------------------------------------------------------
# Проверка типа файла и Zip-бомб
# ----------------------------------------------------------------------
async def is_file_safe(file_path: str, ext: str) -> bool:
    """
    Проверяет файл на соответствие типу (magic) и отсутствие признаков Zip-бомбы.
    """
    # 1. Проверка MIME-типа через magic
    mime = magic.from_file(file_path, mime=True)

    valid_mime_map = {
        '.pdf': 'application/pdf',
        '.doc': 'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg'
    }

    if ext.lower() in valid_mime_map:
        if mime != valid_mime_map[ext.lower()]:
            logging.error(f"MIME mismatch! Expected {valid_mime_map[ext.lower()]}, got {mime}")
            return False
    else:
        return False

    # 2. Проверка на Zip-бомбы (только для DOCX, так как это ZIP)
    if ext.lower() == '.docx':
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                if len(zf.namelist()) > MAX_ZIP_ENTRIES:
                    logging.error("Zip-bomb detected: too many files")
                    return False
                total_size = sum(file.file_size for file in zf.infolist())
                if total_size > MAX_UNCOMPRESSED_SIZE:
                    logging.error(f"Zip-bomb detected: too large ({total_size} bytes)")
                    return False
        except zipfile.BadZipFile:
            logging.error("File is not a valid zip")
            return False

    return True


# ----------------------------------------------------------------------
# Антивирусная проверка через ClamAV
# ----------------------------------------------------------------------
def get_clamav_client():
    try:
        client = pyclamd.ClamdNetworkSocket(CLAMAV_HOST, CLAMAV_PORT)
        client.ping()
        return client
    except Exception as e:
        logging.error(f"ClamAV connection failed: {e}")
        return None


cd = None


async def scan_file(file_path: str) -> bool:
    global cd
    if cd is None:
        cd = get_clamav_client()
        if cd is None:
            logging.error("ClamAV check skipped: Daemon unavailable")
            return True  # Или False, если политика строгая

    try:
        abs_path = os.path.abspath(file_path)
        result = await asyncio.to_thread(cd.scan_file, abs_path)
        if result:
            logging.warning(f"Virus detected: {result}")
            return False
        return True
    except Exception as e:
        logging.error(f"ClamAV scanning error: {e}")
        return True


# ----------------------------------------------------------------------
# Подсчёт страниц
# ----------------------------------------------------------------------
async def get_pdf_page_count(file_path: str) -> int:
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            content = await f.read()
            pdf = PdfReader(BytesIO(content))
            return len(pdf.pages)
    except Exception as e:
        logging.error(f"PDF page count error: {e}")
        return 0


async def get_docx_page_count_metadata(file_path: str) -> int:
    try:
        with zipfile.ZipFile(file_path, 'r') as document:
            dxml = document.read('docProps/app.xml')
            uglyXml = xml.dom.minidom.parseString(dxml)
            page_element = uglyXml.getElementsByTagName('Pages')[0]
            page_count = int(page_element.childNodes[0].nodeValue)
            if page_count is None or page_count < 1:
                raise ValueError("No valid page count in metadata")
            return page_count
    except Exception as e:
        logging.error(f"DOCX metadata page count error: {e}")
        raise


async def get_word_page_count_via_libreoffice(file_path: str) -> int:
    """
    Точный подсчет страниц Word документов через LibreOffice (версия для Linux)
    """
    # В Linux команда обычно доступна просто как 'libreoffice' или 'soffice'
    libreoffice_bin = "libreoffice"
    temp_dir = None

    try:
        temp_dir = tempfile.mkdtemp()

        # В Linux LibreOffice создает PDF с тем же именем, что и оригинал
        base_name = os.path.basename(file_path)
        file_name_without_ext = os.path.splitext(base_name)[0]
        pdf_output_path = os.path.join(temp_dir, f"{file_name_without_ext}.pdf")

        # Команда для Linux.
        # Добавляем параметр -env для изоляции профиля пользователя (нужно для стабильности на сервере)
        cmd = [
            libreoffice_bin,
            '--headless',
            f'-env:UserInstallation=file://{temp_dir}/profile',
            '--convert-to', 'pdf',
            '--outdir', temp_dir,
            file_path
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logging.error(f"LibreOffice failed: {stderr.decode()}")
            return 0

        if not os.path.exists(pdf_output_path):
            logging.error(f"PDF not found at {pdf_output_path}")
            return 0

        page_count = await get_pdf_page_count(pdf_output_path)

        # Очистка
        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

        return page_count or 0

    except Exception as e:
        logging.error(f"Linux LibreOffice error: {str(e)}")
        return 0


async def get_page_count(file_path: str, ext: str) -> int:
    try:
        if ext in ('.png', '.jpg', '.jpeg'):
            return 1
        if ext == '.pdf':
            return await get_pdf_page_count(file_path)
        if ext == '.docx':
            try:
                return await get_docx_page_count_metadata(file_path)
            except Exception:
                return await get_word_page_count_via_libreoffice(file_path)
        if ext == '.doc':
            # Для старых DOC используем LibreOffice
            return await get_word_page_count_via_libreoffice(file_path)
        return 0
    except Exception as e:
        logging.error(f"Page count error: {e}")
        return 0