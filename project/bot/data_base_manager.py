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

db_config = {
    'user': 'root',
    'password': '3465',
    'host': 'localhost',
    'database': 'send_to_print',
    'raise_on_warnings': True
}


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
        INSERT INTO `order` (ID_shop, price, note, con_code, file, color, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """

        # Чтение файла в бинарном формате
        file_path = os.path.join(os.getcwd(), order_data['file_path'])

        # Логирование пути к файлу
        logging.info(f"Путь к файлу: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Файл {file_path} не найден.")

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
            'получен'  # status (статус заказа)
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

