
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

db_config = {
    'user': 'root',
    'password': '3465',
    'host': 'localhost',
    'database': 'send_to_print',
    'raise_on_warnings': True
}

# Явно указываем абсолютный путь и создаем папку
output_dir = os.path.abspath('C:\\python_projects\\send-to-print\\project\\desktop\\for_file')
os.makedirs(output_dir, exist_ok=True)


def save_to_db(order_data):
    """
    Сохраняет данные заказа в базу данных.
    """
    try:
        # Логирование параметров подключения к базе данных
        logging.info(f"Подключение к базе данных: {db_config}")

        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()

        query = """
        INSERT INTO `order` (ID_shop, price, note, con_code, file, color, status, file_extension)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """

        #  1. Чтение файла в бинарном формате
        file_path = os.path.join(os.getcwd(), order_data['file_path'])

        # Логирование пути к файлу
        logging.info(f"Путь к файлу: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Файл {file_path} не найден.")

            # 2. Получаем расширение файла из имени
            _, file_extension = os.path.splitext(file_path)
            file_extension = file_extension.lstrip('.')  # Удаляем точку (".pdf" → "pdf")

        with open(file_path, 'rb') as file:
            file_data = file.read()

        # Подготовка данных для вставки
        values = (
            int(order_data['point']),  # ID_shop (точка)
            Decimal(order_data['cost']),  # price (стоимость)
            order_data.get('comment', 'нет'),  # note (комментарий)
            int(order_data['check_number']),  # con_code (код заказа)
            file_data,  # file (бинарные данные файла)
            order_data['color'],  # color (цвет печати)
            'получен',  # status (статус заказа)
            file_extension.lower()
        )

        # Логирование данных для вставки
        logging.info(f"Данные для вставки: {values}")

        cursor.execute(query, values)
        connection.commit()
        logging.info(f"Заказ {order_data['check_number']} сохранен в базу данных.")

    except mysql.connector.Error as err:
        logging.error(f"Ошибка MySQL: {err}")
        logging.error(f"SQL запрос: {query}")
        logging.error(f"Значения: {values}")
        raise

    except FileNotFoundError as err:
        logging.error(f"Ошибка файла: {err}")
        raise

    except Exception as err:
        logging.error(f"Ошибка при сохранении заказа в базу данных: {err}", exc_info=True)
        raise

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'connection' in locals():
            connection.close()


def download_blob():
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT ID, ID_shop, price, note, con_code, file, color, status, file_extension 
            FROM `order` 
            WHERE status = 'получен'
        """)

        orders = cursor.fetchall()

        for order in orders:
            if order['file'] and order['file'] is not None:
                try:
                    # Безопасное получение расширения файла
                    ext = 'bin'  # Значение по умолчанию
                    if order.get('file_extension'):
                        ext = str(order['file_extension']).lstrip('.')

                    filename = f"order_{order['ID']}.{ext}"
                    filepath = os.path.join(output_dir, filename)

                    # Защищенная запись файла
                    with open(filepath, 'wb') as f:
                        if isinstance(order['file'], bytes):
                            f.write(order['file'])
                        else:
                            f.write(order['file'].encode())

                    order['file_path'] = filepath  # Сохраняем абсолютный путь

                except Exception as e:
                    logging.error(f"Error saving file {order.get('ID', '?')}: {str(e)}")
                    continue

        return orders

    except Exception as e:
        logging.error(f"Database error: {str(e)}")
        return []
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'connection' in locals(): connection.close()




