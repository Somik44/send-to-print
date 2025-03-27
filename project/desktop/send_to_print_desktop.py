import sys
import os
import subprocess
import time
import logging
from PyQt5 import QtWidgets
from PyQt5.QtWidgets import (
    QVBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QWidget, QHBoxLayout, QMessageBox
)
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from data_base_manager import save_to_db


class DatabaseLoader(QThread):
    data_loaded = pyqtSignal(list)

    def __init__(self):
        super().__init__()

    def run(self):
        from data_base_manager import download_blob
        try:
            orders = download_blob()
            self.data_loaded.emit(orders)
        except Exception as e:
            logging.error(f"Ошибка загрузки данных: {e}")
            self.data_loaded.emit([])


class OrderProcessor(QThread):
    order_received = pyqtSignal(dict)

    def run(self):
        while True:
            time.sleep(1)
            with order_lock:
                if order_queue:
                    order_data = order_queue.pop(0)
                    self.order_received.emit(order_data)


class FileReceiverApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.start_order_processor()
        self.load_initial_data()

    def load_initial_data(self):
        """Загружает начальные данные из БД."""
        self.loader = DatabaseLoader()
        self.loader.data_loaded.connect(self.handle_loaded_data)
        self.loader.start()

    def handle_loaded_data(self, orders):
        """Обрабатывает загруженные данные из БД."""
        if not orders:
            return

        for order in orders:
            order_data = {
                'order_number': order['ID'],
                'file_path': os.path.abspath(order.get('file_path', '')),
                'color': order.get('color', 'Неизвестно'),
                'cost': str(order.get('price', '0')),
                'check_number': order.get('con_code', ''),
                'comment': order.get('note', 'Нет комментария'),
                'point': order.get('ID_shop', '0')
            }
            self.update_received_list(order_data)

    def initUI(self):
        self.setWindowIcon(QIcon("D:\\проект Отправь на печать и забери!\\logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        self.layout = QVBoxLayout()

        self.received_label = QLabel('Полученные файлы:')
        self.layout.addWidget(self.received_label)
        self.received_list = QListWidget()
        self.layout.addWidget(self.received_list)

        self.ready_label = QLabel('Готовые к выдаче файлы:')
        self.layout.addWidget(self.ready_label)
        self.ready_list = QListWidget()
        self.layout.addWidget(self.ready_list)

        font = QFont("San Francisco", 10)
        self.received_label.setFont(font)
        self.ready_label.setFont(font)
        self.received_list.setFont(font)
        self.ready_list.setFont(font)

        self.setLayout(self.layout)

    def start_order_processor(self):
        self.order_processor = OrderProcessor()
        self.order_processor.order_received.connect(self.update_received_list)
        self.order_processor.start()

    def update_received_list(self, order_data):
        file_name = order_data['file_path']
        order_info = order_data

        for i in range(self.received_list.count()):
            item = self.received_list.item(i)
            widget = self.received_list.itemWidget(item)
            label = widget.findChild(QLabel)
            if label and file_name in label.text():
                return

        item = QListWidgetItem()
        h_layout = QHBoxLayout()

        label = QLabel(f"Заказ №{order_info['order_number']}: {file_name}")
        label.setStyleSheet('color: red')
        label.setFont(QFont("Arial", 12))
        h_layout.addWidget(label)

        button = QPushButton('Готов к выдаче')
        button.clicked.connect(lambda: self.move_to_ready(file_name, item))
        button.setFixedSize(180, 30)
        h_layout.addWidget(button)

        info_button = QPushButton('Подробная информация')
        info_button.clicked.connect(lambda: self.show_file_info(file_name))
        info_button.setFixedSize(220, 30)
        h_layout.addWidget(info_button)

        widget = QWidget()
        widget.setLayout(h_layout)
        widget.setMinimumHeight(50)
        widget.setMaximumHeight(50)

        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)
        self.received_list.setSpacing(10)

    def show_file_info(self, file_name):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        info_text = "\n".join([
            f"Имя файла: {file_name}",
            f"Цвет печати: {order_info.get('color', 'Неизвестно')}",
            f"Количество страниц: {order_info.get('pages', 'Неизвестно')}",
            f"Стоимость: {order_info.get('cost', 'Неизвестно')} руб.",
            f"Проверочный код: {order_info.get('check_number', 'Неизвестно')}",
            f"Комментарий: {order_info.get('comment', 'Нет комментария')}"
        ])
        QMessageBox.information(self, "Информация о файле", info_text)

    def move_to_ready(self, file_name, item):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if order_info:
            bot.send_message(order_info['user_id'], "Ваш заказ готов к выдаче.")

        row = self.received_list.row(item)
        self.received_list.takeItem(row)

        ready_item = QListWidgetItem()
        h_layout = QHBoxLayout()

        label = QLabel(f"Заказ №{order_info['order_number']} готов: {file_name}")
        label.setStyleSheet('color: green')
        label.setFont(QFont("Arial", 12))
        h_layout.addWidget(label)

        print_button = QPushButton('Печать')
        print_button.clicked.connect(lambda: self.print_file(file_name))
        print_button.setFixedSize(100, 30)
        h_layout.addWidget(print_button)

        show_code_button = QPushButton('Показать код выдачи')
        show_code_button.clicked.connect(lambda: self.show_code(file_name))
        show_code_button.setFixedSize(185, 30)
        h_layout.addWidget(show_code_button)

        ready_widget = QWidget()
        ready_widget.setLayout(h_layout)
        ready_widget.setMinimumHeight(50)
        ready_widget.setMaximumHeight(50)

        self.ready_list.insertItem(0, ready_item)
        self.ready_list.setItemWidget(ready_item, ready_widget)
        self.ready_list.setSpacing(10)

    def show_code(self, file_name):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if not order_info:
            return

        msg_box = QMessageBox()
        msg_box.setWindowTitle("Код заказа")
        msg_box.setText(f"Код заказа: {order_info['check_number']}")
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

        if msg_box.exec_() == QMessageBox.Ok:
            file_path = file_name  # Используем полный путь, который уже пришел из БД
            try:
                os.remove(file_path)
                QMessageBox.information(self, "Успех", f"Файл {file_name} удалён.")
                bot.send_message(order_info['user_id'], "Заказ получен. Спасибо!")
            except Exception as e:
                logging.error(f"Ошибка удаления файла: {str(e)}")

    def print_file(self, file_name):
        file_path = file_name  # Используем полный путь из данных заказа
        if not os.path.isfile(file_path):
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Файл не найден: {file_path}")
            return

        try:
            if file_name.lower().endswith('.pdf'):
                subprocess.run(["C:\\Program Files\\Adobe\\Acrobat DC\\Acrobat\\Acrobat.exe", "/p", file_path])
            elif file_name.lower().endswith(('.docx', '.doc')):
                subprocess.run(["C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE", "/p", file_path])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось напечатать файл: {str(e)}")


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    receiver = FileReceiverApp()
    receiver.show()
    sys.exit(app.exec_())