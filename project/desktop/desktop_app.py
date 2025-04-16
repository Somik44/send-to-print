import sys
import os
import requests
import logging
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton, QLabel, QMessageBox, QHBoxLayout, QListWidgetItem, QFontDialog)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QIcon, QFont
import subprocess
import time
import win32com.client
from datetime import datetime

API_URL = "http://localhost:5000/api"
DOWNLOAD_DIR = os.path.abspath('C:\\send_to_ptint\\send-to-print\\project\\desktop\\dowlands')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='app.log'
)


class OrderLoader(QThread):
    data_loaded = pyqtSignal(list)

    def run(self):
        while True:
            try:
                response = requests.get(f"{API_URL}/orders?status=received")
                if response.ok:
                    self.data_loaded.emit(response.json())
                self.msleep(10000)
            except Exception as e:
                logging.error(f"Ошибка загрузки: {str(e)}")
                self.msleep(12000)


class UpdateOrderThread(QThread):
    finished = pyqtSignal(bool, dict)

    def __init__(self, order_id, new_status):
        super().__init__()
        self.order_id = order_id
        self.new_status = new_status

    def run(self):
        try:
            response = requests.put(
                f"{API_URL}/orders/{self.order_id}",
                json={'status': self.new_status}
            )
            self.finished.emit(response.ok, response.json())
        except Exception as e:
            logging.error(f"Ошибка обновления: {str(e)}")
            self.finished.emit(False, {})


class FileReceiverApp(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.loader = OrderLoader()
        self.loader.data_loaded.connect(self.update_received_list)
        self.loader.start()
        self.current_order = None

    def initUI(self):
        self.setWindowTitle('Send to print and pick up!')
        self.setWindowIcon(QIcon("logo.png"))
        self.setFixedSize(1000, 800)

        layout = QVBoxLayout()

        # Стилизация текста
        font = QFont("San Francisco", 10)

        # Секция полученных заказов
        self.received_label = QLabel('Полученные файлы:')
        self.received_label.setFont(font)
        layout.addWidget(self.received_label)

        self.received_list = QListWidget()
        self.received_list.setFont(font)
        self.received_list.setSpacing(10)
        layout.addWidget(self.received_list)

        # Секция готовых заказов
        self.ready_label = QLabel('Готовые к выдаче файлы:')
        self.ready_label.setFont(font)
        layout.addWidget(self.ready_label)

        self.ready_list = QListWidget()
        self.ready_list.setFont(font)
        self.ready_list.setSpacing(10)
        layout.addWidget(self.ready_list)

        self.setLayout(layout)
        self.setStyleSheet("""
            QListWidget { font-size: 14px; }
            QPushButton { 
                min-width: 80px; 
                padding: 5px;
                background-color: #4CAF50;
                color: white;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #45a049; }
        """)

    def update_received_list(self, orders):
        current_ids = {self.received_list.itemWidget(item).property("id")
                       for item in (self.received_list.item(i) for i in range(self.received_list.count()))}

        for order in orders:
            order_id = str(order['ID'])
            if order_id not in current_ids:
                self.add_order_to_list(order)

    def add_order_to_list(self, order):
        item = QListWidgetItem()
        widget = self.create_order_widget(order)
        item.setSizeHint(widget.sizeHint())
        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)

    def create_order_widget(self, order):
        widget = QWidget()
        layout = QHBoxLayout()

        # Информация о заказе
        label_text = f"Заказ №{order['ID']}: {order['file_path']}"
        label = QLabel(label_text)
        label.setStyleSheet("color: red; margin-left: 10px;")
        label.setFont(QFont("Arial", 12))

        # Кнопки действий
        btn_ready = QPushButton("Готов к выдаче")
        btn_ready.setFixedSize(180, 30)
        btn_ready.clicked.connect(lambda: self.start_update_order(order, 'готов'))

        btn_info = QPushButton("Подробная информация")
        btn_info.setFixedSize(220, 30)
        btn_info.clicked.connect(lambda: self.show_file_info(order))

        layout.addWidget(label)
        layout.addWidget(btn_ready)
        layout.addWidget(btn_info)
        widget.setLayout(layout)
        widget.setProperty("id", str(order['ID']))
        return widget

    def show_file_info(self, order):
        info_text = (
            f"Имя файла: {order['file_path']}\n"
            f"Цвет печати: {order.get('color', 'Неизвестно')}\n"
            f"Страниц: {order.get('pages', 'Неизвестно')}\n"
            f"Стоимость: {order.get('price', 'Неизвестно')} руб.\n"
            f"Код: {order.get('con_code', 'Неизвестно')}\n"
            f"Комментарий: {order.get('note', 'Нет комментария')}"
        )
        QMessageBox.information(self, "Информация о заказе", info_text)

    def start_update_order(self, order, new_status):
        self.current_order = order
        self.thread = UpdateOrderThread(order['ID'], new_status)
        self.thread.finished.connect(self.handle_update_result)
        self.thread.start()

    def handle_update_result(self, success, response):
        if success:
            self.move_to_ready(self.current_order)
            QMessageBox.information(self, "Успех", "Статус обновлен")
        else:
            QMessageBox.critical(self, "Ошибка", "Ошибка обновления")

    def move_to_ready(self, order):
        # Удаление из полученных
        for i in range(self.received_list.count()):
            item = self.received_list.item(i)
            if self.received_list.itemWidget(item).property("id") == str(order['ID']):
                self.received_list.takeItem(i)
                break

        # Добавление в готовые
        item = QListWidgetItem()
        widget = self.create_ready_widget(order)
        item.setSizeHint(widget.sizeHint())
        self.ready_list.addItem(item)
        self.ready_list.setItemWidget(item, widget)

    def create_ready_widget(self, order):
        widget = QWidget()
        layout = QHBoxLayout()

        label = QLabel(f"Заказ №{order['ID']} готов: {order['file_path']}")
        label.setStyleSheet("color: green; margin-left: 10px;")
        label.setFont(QFont("Arial", 12))

        btn_print = QPushButton("Печать")
        btn_print.setFixedSize(100, 30)
        btn_print.clicked.connect(lambda: self.print_file(order['file_path']))

        btn_code = QPushButton("Показать код")
        btn_code.setFixedSize(185, 30)
        btn_code.clicked.connect(lambda: self.show_code(order))

        btn_order_end = QPushButton("Заказ выдан")
        btn_code.setFixedSize(185, 30)
        btn_order_end.clicked.connect(lambda: self.order_end(order))

        layout.addWidget(label)
        layout.addWidget(btn_print)
        layout.addWidget(btn_code)
        layout.addWidget(btn_order_end)
        widget.setLayout(layout)
        return widget

    def print_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            # Скачивание файла
            response = requests.get(f"{API_URL}/files/{filename}", stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Печать
            try:
                os.startfile(filepath)
            except Exception as e:
                print(f"Ошибка при открытии файла: {str(e)}")

            # Удаление через 30 сек
            # time.sleep(30)
            # if os.path.exists(filepath):
            #     os.remove(filepath)

        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Ошибка: {str(e)}")

    def show_code(self, order):
        msg = QMessageBox()
        msg.setWindowTitle("Код заказа")
        msg.setText(f"Код для получения: {order['con_code']}")
        msg.exec_()

    def order_end(self, order):
        filename = order['file_path']  # Получаем имя файла из объекта заказа
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                print(f"Файл {filename} успешно удален.")
                self.remove_order_widget(order)  # Удаляем виджет, передавая объект заказа
            except Exception as e:
                print(f"Ошибка при удалении файла: {str(e)}")
        else:
            print(f"Файл {filename} не найден.")

    def remove_order_widget(self, order):
        for i in range(self.ready_list.count()):
            item = self.ready_list.item(i)
            # Проверяем, соответствует ли текст метки номеру заказа
            if self.ready_list.itemWidget(item).findChild(
                    QLabel).text() == f"Заказ №{order['ID']} готов: {order['file_path']}":
                self.ready_list.takeItem(i)
                break


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = FileReceiverApp()
    window.show()
    sys.exit(app.exec_())