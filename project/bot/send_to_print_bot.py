import queue
import mysql.connector
import threading
import os
import random
import logging
import telebot
from PyPDF2 import PdfReader
import pythoncom
import win32com.client
from decimal import Decimal
from data_base_manager import save_to_db

API_TOKEN = '7818669005:AAFyAMagVNx7EfJsK-pVLUBkGLfmMp9J2EQ'
bot = telebot.TeleBot(API_TOKEN)
user_states = {}
order_codes = {}
order_queue = queue.Queue()  # Очередь заказов
order_lock = threading.Lock()  # Блокировка для обработки заказов
order_number_lock = threading.Lock()  # Блокировка для изменения order_number
order_number = 0

def get_pdf_page_count(file_path):
    """
    Подсчитывает количество страниц в PDF файле.
    """
    try:
        with open(file_path, 'rb') as file:
            reader = PdfReader(file)
            return len(reader.pages)
    except Exception as e:
        logging.error(f"Ошибка при подсчете страниц в PDF файле: {e}")
        return None

def get_docx_page_count(file_path):
    """
    Подсчитывает количество страниц в DOCX файле.
    """
    try:
        pythoncom.CoInitialize()
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(file_path)
        page_count = doc.ComputeStatistics(2)
        doc.Close(False)
        return page_count
    except Exception as e:
        logging.error(f"Ошибка при подсчете страниц в DOCX файле: {e}")
        return None
    finally:
        word.Quit()
        pythoncom.CoUninitialize()

def get_doc_page_count(file_path):
    """
    Подсчитывает количество страниц в DOC файле.
    """
    pythoncom.CoInitialize()
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(file_path)
        page_count = doc.ComputeStatistics(2)
        doc.Close(False)
        return page_count
    except Exception as e:
        logging.error(f"Ошибка при подсчете страниц: {e}")
        return None
    finally:
        word.Quit()
        pythoncom.CoUninitialize()

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.")
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    markup.row('1')  # Только одна точка
    bot.send_message(message.chat.id, "Выберите точку (введите цифру):", reply_markup=markup)
    bot.register_next_step_handler(message, process_point_selection)

def process_point_selection(message):
    """
    Обрабатывает выбор точки и сохраняет его в user_states.
    """
    point = message.text.strip()
    if point == '1':
        user_states[message.chat.id] = {'point': point, 'state': None}
        bot.send_message(message.chat.id, f"Вы выбрали точку №{point}. Теперь вы можете начать новый заказ, используя команду /new_order.")
    else:
        bot.send_message(message.chat.id, "Некорректный выбор точки. Пожалуйста, выберите точку 1.")
        send_welcome(message)  # Повторно предлагаем выбрать точку

@bot.message_handler(commands=['new_order'])
def new_order(message):
    """
    Обрабатывает команду /new_order.
    """
    if 'point' not in user_states.get(message.chat.id, {}):
        bot.send_message(message.chat.id, "Сначала выберите точку, используя команду /start.")
        return

    bot.send_message(message.chat.id, "Отправь только один PDF, DOCX или DOC файл (не более 20 МБ), а бот рассчитает его стоимость.")
    bot.send_message(message.chat.id, "Прайс лист: 5 рублей/страница - Черно-белый формат; 15 рублей/страница - Цветной формат")
    user_states[message.chat.id]['state'] = 'awaiting_file'

@bot.message_handler(commands=['reset'])
def reset(message):
    """
    Обрабатывает команду /reset.
    """
    user_states.pop(message.chat.id, None)
    bot.send_message(message.chat.id, "Состояние сброшено. Вы можете начать новый заказ, используя команду /new_order.")

@bot.message_handler(func=lambda message: True)
def handle_unknown_message(message):
    """
    Обрабатывает неизвестные команды.
    """
    bot.send_message(message.chat.id, "Не понимаю тебя, попробуй повторить запрос :)")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    """
    Обрабатывает загруженные документы.
    """
    try:
        if user_states.get(message.chat.id, {}).get('state') != 'awaiting_file':
            bot.reply_to(message, "Сначала нажмите /new_order, чтобы начать прием файлов.")
            return

        if message.document.mime_type not in [
            'application/pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'application/msword'
        ]:
            bot.reply_to(message, "Неподдерживаемый формат документа. Пожалуйста, отправьте PDF, DOCX или DOC.")
            return

        global order_number
        with order_number_lock:
            current_order_number = order_number
            order_number += 1

        code_order = random.randint(10000, 99999)
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        original_name = message.document.file_name
        name, extension = os.path.splitext(original_name)
        final_file_name = f"file_{current_order_number}{extension}"
        final_file_path = os.path.join(os.getcwd(), final_file_name)

        counter = 1
        while os.path.exists(final_file_path):
            final_file_name = f"file_{current_order_number}_{counter}{extension}"
            final_file_path = os.path.join(os.getcwd(), final_file_name)
            counter += 1

        with open(final_file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        processing_message = bot.send_message(message.chat.id, "Файл обрабатывается, пожалуйста подождите...")

        pages = None
        if extension.lower() == '.pdf':
            pages = get_pdf_page_count(final_file_path)
        elif extension.lower() in ('.docx', '.doc'):
            pages = get_docx_page_count(final_file_path)

        bot.delete_message(message.chat.id, processing_message.message_id)

        if not pages:
            bot.reply_to(message, "Ошибка обработки файла")
            os.remove(final_file_path)
            return

        order_data = {
            'point': user_states[message.chat.id]['point'],
            'user_id': message.chat.id,
            'file_path': final_file_name,
            'check_number': code_order,
            'pages': pages,
            'extension': extension,
        }

        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        markup.row('Черно-белая', 'Цветная')

        bot.send_message(
            message.chat.id,
            f"Количество страниц: {pages}\n"
            "Выберите тип печати:",
            reply_markup=markup
        )

        bot.register_next_step_handler(message, lambda msg: process_color_selection(msg, order_data))

    except Exception as e:
        logging.error(f"Document handler error: {e}")
        bot.reply_to(message, "Произошла ошибка при обработке файла")

def process_color_selection(message, order_data):
    """
    Обрабатывает выбор цвета печати.
    """
    try:
        remove_keyboard = telebot.types.ReplyKeyboardRemove()
        color_choice = message.text.lower()

        if 'черно-белая' in color_choice:
            order_data['color'] = 'черно-белая'
            order_data['cost'] = order_data['pages'] * 5  # 5 рублей за страницу
        elif 'цветная' in color_choice:
            order_data['color'] = 'цветная'
            order_data['cost'] = order_data['pages'] * 15  # 15 рублей за страницу
        else:
            raise ValueError("Некорректный выбор цвета")

        bot.send_message(message.chat.id, "Комментарий к заказу (поставьте $ если комментарий не нужен):",
                         reply_markup=remove_keyboard)
        bot.register_next_step_handler(message, lambda msg: process_comment(msg, order_data))

    except Exception as e:
        logging.error(f"Color selection error: {e}")
        bot.send_message(message.chat.id, "Ошибка выбора цвета, начните заново",
                         reply_markup=telebot.types.ReplyKeyboardRemove())

def process_comment(message, order_data):
    """
    Обрабатывает комментарий к заказу.
    """
    try:
        comment = message.text.strip()
        if comment != '$':
            order_data['comment'] = comment
        else:
            order_data['comment'] = None

        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        markup.row('Подтвердить', 'Отменить')

        bot.send_message(
            message.chat.id,
            f"Страниц: {order_data['pages']}\n"
            f"Цвет: {order_data['color']}\n"
            f"Сумма: {order_data['cost']} руб.\n"
            f"Комментарий: {order_data.get('comment', 'Нет комментария')}\n\n"
            "Подтвердите заказ:",
            reply_markup=markup
        )

        bot.register_next_step_handler(message, lambda msg: handle_confirmation(msg, order_data))

    except Exception as e:
        logging.error(f"Comment processing error: {e}")
        bot.send_message(message.chat.id, "Ошибка обработки комментария",
                         reply_markup=telebot.types.ReplyKeyboardRemove())
        file_path = os.path.join(os.getcwd(), order_data['file_path'])
        if os.path.exists(file_path):
            os.remove(file_path)

def handle_confirmation(message, order_data):
    """
    Обрабатывает подтверждение заказа.
    """
    try:
        remove_keyboard = telebot.types.ReplyKeyboardRemove()
        choice = message.text.lower()

        if 'подтвердить' in choice:
            global order_number
            with order_number_lock:
                current_order_number = order_number
                order_number += 1

            order_data['order_number'] = current_order_number
            order_codes[current_order_number] = order_data
            order_queue.put(order_data)

            # Сохраняем заказ в базу данных
            save_to_db(order_data)

            bot.send_message(
                message.chat.id,
                f"Заказ №{current_order_number} принят!\n"
                f"Код для получения: {order_data['check_number']}",
                reply_markup=remove_keyboard
            )
        elif 'отменить' in choice:
            file_path = os.path.join(os.getcwd(), order_data['file_path'])
            if os.path.exists(file_path):
                os.remove(file_path)
            bot.send_message(
                message.chat.id,
                "Заказ отменен",
                reply_markup=remove_keyboard
            )
        else:
            raise ValueError("Некорректный выбор подтверждения")

    except Exception as e:
        logging.error(f"Ошибка при обработке подтверждения: {e}")
        bot.send_message(
            message.chat.id,
            "Произошла ошибка при обработке заказа. Пожалуйста, попробуйте снова.",
            reply_markup=remove_keyboard
        )
        file_path = os.path.join(os.getcwd(), order_data['file_path'])
        if os.path.exists(file_path):
            os.remove(file_path)

def start_bot():
    """
    Запускает бота.
    """
    bot.polling(none_stop=True)

if __name__ == '__main__':
    start_bot()
