import os
import logging
import random
import threading
import mysql.connector.pooling
from telebot import TeleBot, types
from PyPDF2 import PdfReader
import pythoncom
import win32com.client
from decimal import Decimal
import uuid
import traceback
import time

logging.basicConfig(
    level=logging.DEBUG,
    filename='bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

API_TOKEN = '7818669005:AAFyAMagVNx7EfJsK-pVLUBkGLfmMp9J2EQ'
UPLOAD_FOLDER = os.path.abspath('D:\\projects_py\\projectsWithGit\\send-to-print\\project\\api\\uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DB_CONFIG = {
    'user': 'root',
    'password': 'Qwerty123',
    'host': 'localhost',
    'database': 'send_to_print',
    'pool_name': 'bot_pool',
    'pool_size': 10
}

db_pool = mysql.connector.pooling.MySQLConnectionPool(**DB_CONFIG)
user_states = {}
timers = {}

bot = TeleBot(API_TOKEN)


def get_db_connection():
    return db_pool.get_connection()


def cleanup_resources(chat_id):
    if chat_id in user_states:
        temp_path = user_states[chat_id].get('temp_file')
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        del user_states[chat_id]
    if chat_id in timers:
        timers[chat_id].cancel()
        del timers[chat_id]


def start_inactivity_timer(chat_id):
    def reset_state():
        cleanup_resources(chat_id)
        bot.send_message(chat_id, "🚫 Сессия закрыта из-за неактивности", reply_markup=types.ReplyKeyboardRemove())

    if chat_id in timers:
        timers[chat_id].cancel()
    timers[chat_id] = threading.Timer(60.0, reset_state)
    timers[chat_id].start()


def get_pdf_page_count(file_path):
    try:
        with open(file_path, 'rb') as f:
            return len(PdfReader(f).pages)
    except Exception as e:
        logging.error(f"PDF Error: {e}")
        return None


def get_docx_page_count(file_path):
    try:
        pythoncom.CoInitialize()
        word = win32com.client.Dispatch("Word.Application")
        doc = word.Documents.Open(os.path.abspath(file_path))
        count = doc.ComputeStatistics(2)
        doc.Close(False)
        return count
    except Exception as e:
        logging.error(f"DOCX Error: {e}")
        return None
    finally:
        word.Quit()
        pythoncom.CoUninitialize()


def get_doc_page_count(file_path):
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
def handle_start(message):
    bot.send_message(
        message.chat.id,
        f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.")



@bot.message_handler(commands=['new_order'])
def handle_new_order(message):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name FROM shop")
        shops = cursor.fetchall()

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for shop in shops:
            markup.add(types.KeyboardButton(shop['name']))

        bot.send_message(message.chat.id, "🏪 Выберите точку печати из списка:", reply_markup=markup)
        start_inactivity_timer(message.chat.id)
        bot.register_next_step_handler(message, process_shop_selection)

    except Exception as e:
        logging.error(f"Ошибка при выборе точки: {traceback.format_exc()}")
        bot.send_message(message.chat.id, "❌ Ошибка загрузки точек")
    finally:
        cursor.close()
        conn.close()


def process_shop_selection(message):
    try:
        selected_name = message.text.strip()
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT ID_shop, name, address, price_bw, price_cl FROM shop WHERE name = %s",
            (selected_name,))
        shop = cursor.fetchone()

        if shop:
            user_states[message.chat.id] = {
                'state': 'awaiting_file',
                'shop_id': shop['ID_shop'],
                'shop_info': {
                    'name': shop['name'],
                    'address': shop['address'],
                    'price_bw': shop['price_bw'],
                    'price_cl': shop['price_cl']
                }
            }

            response = (
                f"🏪 Выбрана точка: {shop['name']}\n"
                f"📍 Адрес: {shop['address']}\n"
                f"💰 Цены:\n"
                f"• Черно-белая: {shop['price_bw']} руб/стр\n"
                f"• Цветная: {shop['price_cl']} руб/стр\n\n"
                f"📎 Отправьте файл для расчета стоимости."
            )

            bot.send_message(message.chat.id, response, reply_markup=types.ReplyKeyboardRemove())
            start_inactivity_timer(message.chat.id)
            bot.register_next_step_handler(message, handle_document)

        else:
            bot.send_message(message.chat.id, "❌ Точка не найдена. /new_order")

    except Exception as e:
        logging.error(f"Ошибка выбора точки: {traceback.format_exc()}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



@bot.message_handler(content_types=['document'])
def handle_document(message):
    processing_msg = None
    try:
        user_state = user_states.get(message.chat.id, {})
        if user_state.get('state') != 'awaiting_file':
            raise ValueError("⚠️ Сначала выберите точку через /new_order")

        processing_msg = bot.send_message(message.chat.id, "⏳ Файл обрабатывается, подождите пожалуйста...")
        start_inactivity_timer(message.chat.id)

        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        filename, ext = os.path.splitext(message.document.file_name)
        ext = ext.lower()

        if ext not in ['.pdf', '.doc', '.docx']:
            raise ValueError("❌ Поддерживаются только PDF/DOC/DOCX")

        temp_name = f"temp_{uuid.uuid4()}{ext}"
        temp_path = os.path.join(UPLOAD_FOLDER, temp_name)
        with open(temp_path, 'wb') as f:
            f.write(downloaded)

        pages = get_pdf_page_count(temp_path) if ext == '.pdf' else get_doc_page_count(temp_path)
        if not pages or pages < 1:
            raise ValueError("⚠️ Невозможно определить количество страниц")

        bot.delete_message(message.chat.id, processing_msg.message_id)

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('Черно-белая', 'Цветная')

        user_states[message.chat.id].update({
            'temp_file': temp_path,
            'pages': pages,
            'file_extension': ext[1:],
            'state': 'awaiting_color'
        })

        bot.send_message(message.chat.id, f"📄 Страниц: {pages}\nВыберите тип печати:", reply_markup=markup)
        start_inactivity_timer(message.chat.id)

    except Exception as e:
        if processing_msg:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        bot.reply_to(message, f"❌ Ошибка обработки файла! {str(e)}")
        cleanup_resources(message.chat.id)


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get('state') == 'awaiting_color')
def handle_color(message):
    try:
        user_data = user_states[message.chat.id]
        valid_responses = {
            'черно-белая': user_data['shop_info']['price_bw'],
            'цветная': user_data['shop_info']['price_cl']
        }

        # Нормализуем ввод
        user_input = message.text.strip().lower()

        if user_input not in valid_responses:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add('Черно-белая', 'Цветная')

            bot.send_message(message.chat.id,"❌ Неверный тип печати! Выберите вариант из кнопок ниже:", reply_markup=markup)
            return  # Прерываем выполнение

        # Если выбор корректен
        color_type = user_input
        price = valid_responses[user_input]

        user_data.update({
            'color': color_type,
            'price': user_data['pages'] * price,
            'state': 'awaiting_comment'
        })

        bot.send_message(message.chat.id,"📝 Введите комментарий ($ для пропуска):", reply_markup=types.ReplyKeyboardRemove())
        start_inactivity_timer(message.chat.id)

    except Exception as e:
        logging.error(f"Ошибка: {traceback.format_exc()}")
        bot.reply_to(message, "❌ Критическая ошибка, пожалуйста начните заново /new_order")
        cleanup_resources(message.chat.id)


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get('state') == 'awaiting_comment')
def handle_comment(message):
    try:
        comment = message.text if message.text != '$' else ''
        user_states[message.chat.id]['comment'] = comment
        user_states[message.chat.id]['state'] = 'awaiting_confirm'

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add('Подтвердить', 'Отменить')

        response = (
            f"🔍 Подтвердите заказ:\n"
            f"• Точка: {user_states[message.chat.id]['shop_info']['name']} по адресу {user_states[message.chat.id]['shop_info']['address']}\n"
            f"• Страниц: {user_states[message.chat.id]['pages']}\n"
            f"• Тип: {user_states[message.chat.id]['color']}\n"
            f"• Стоимость: {user_states[message.chat.id]['price']} руб\n"
            f"• Комментарий: {comment if comment else 'NONE'}"
        )

        bot.send_message(message.chat.id, response, reply_markup=markup)
        start_inactivity_timer(message.chat.id)

    except Exception as e:
        logging.error(f"Ошибка комментария: {traceback.format_exc()}")
        bot.reply_to(message, "❌ Ошибка комментария")
        cleanup_resources(message.chat.id)



@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get('state') == 'awaiting_confirm')
def handle_confirm(message):
    conn = None
    cursor = None
    try:
        if message.text == 'Отменить':
            cleanup_resources(message.chat.id)
            temp_path = user_states.get(message.chat.id, {}).get('temp_file')
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            user_states.pop(message.chat.id, None)
            bot.send_message(message.chat.id,"❌ Заказ отменен", reply_markup=types.ReplyKeyboardRemove())
            return

        user_data = user_states[message.chat.id]
        check_code = random.randint(100000, 999999)
        order_data = {
            'shop_id': user_data['shop_id'],
            'price': user_data['price'],
            'comment': user_data.get('comment', ''),
            'check_code': check_code,
            'color': user_data['color'],
            'file_extension': user_data['file_extension'],
            'user_id': message.chat.id,
            'pages': user_data['pages']
        }

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO `order` 
            (ID_shop, price, note, con_code, color, status, user_id, pages, file_extension) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                order_data['shop_id'],
                Decimal(order_data['price']),
                order_data['comment'],
                order_data['check_code'],
                order_data['color'],
                'получен',
                str(order_data['user_id']),
                order_data['pages'],
                order_data['file_extension']
            )
        )
        order_id = cursor.lastrowid
        conn.commit()

        new_filename = f"order_{order_id}.{order_data['file_extension']}"
        new_path = os.path.join(UPLOAD_FOLDER, new_filename)
        os.rename(user_data['temp_file'], new_path)

        cursor.execute(
            "UPDATE `order` SET file_path = %s WHERE ID = %s", (new_filename, order_id))
        conn.commit()

        bot.send_message(message.chat.id,f"✅ Заказ №{order_id} принят! Проверочный код: {check_code}",reply_markup=types.ReplyKeyboardRemove())
        cleanup_resources(message.chat.id)

    except Exception as e:
        logging.error(f"Ошибка подтверждения: {traceback.format_exc()}")
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
        if conn:
            conn.rollback()
        cleanup_resources(message.chat.id)
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



@bot.message_handler(func=lambda msg: True)
def handle_unknown(message):
    bot.reply_to(message, "Не понимаю тебя, попробуй повторить запрос ☺️")



@bot.message_handler(commands=['reset'])
def reset(message):
    user_states.pop(message.chat.id, None)
    bot.send_message(message.chat.id, "Состояние сброшено. Вы можете начать новый заказ, используя команду /new_order.")



if __name__ == '__main__':
    bot.polling(none_stop=True)