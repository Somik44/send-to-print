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
from telegram_bot import order_queue, order_codes, order_lock, bot

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class OrderProcessor(QThread):
    order_received = pyqtSignal(dict)  # Сигнал для передачи данных о заказе

    def run(self):
        while True:
            with order_lock:
                if not order_queue.empty():
                    order_data = order_queue.get()
                    self.order_received.emit(order_data)  # Отправляем сигнал с данными заказа
                    self.process_order(order_data)

    def process_order(self, order_data):
        try:
            file_path = os.path.join(os.getcwd(), order_data['file_path'])
            if os.path.exists(file_path):
                logging.info(f"Обрабатывается заказ: {order_data['file_path']}")
                time.sleep(5)  # Имитация обработки
                logging.info(f"Заказ {order_data['file_path']} обработан.")
        except Exception as e:
            logging.error(f"Ошибка обработки заказа: {e}")

class FileReceiverApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.start_order_processor()

    def initUI(self):
        self.setWindowIcon(QIcon("D:\\проект Отправь на печать и забери!\\logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        # Основной макет
        self.layout = QVBoxLayout()

        # Список полученных файлов
        self.received_label = QLabel('Полученные файлы:')
        self.layout.addWidget(self.received_label)
        self.received_list = QListWidget()
        self.layout.addWidget(self.received_list)

        # Список готовых к выдаче файлов
        self.ready_label = QLabel('Готовые к выдаче файлы:')
        self.layout.addWidget(self.ready_label)
        self.ready_list = QListWidget()
        self.layout.addWidget(self.ready_list)

        # Настройка шрифтов
        font = QFont("San Francisco", 10)
        self.received_label.setFont(font)
        self.ready_label.setFont(font)
        self.received_list.setFont(font)
        self.ready_list.setFont(font)

        self.setLayout(self.layout)

    def start_order_processor(self):
        self.order_processor = OrderProcessor()
        self.order_processor.order_received.connect(self.update_received_list)  # Подключаем сигнал
        self.order_processor.start()

    def update_received_list(self, order_data):
        """
        Обновляет список полученных файлов при поступлении нового заказа.
        """
        file_name = order_data['file_path']
        order_info = order_data
        if not order_info:
            return

        # Проверяем, есть ли уже такой файл в списке
        for i in range(self.received_list.count()):
            item = self.received_list.item(i)
            widget = self.received_list.itemWidget(item)
            label = widget.findChild(QLabel)
            if label and file_name in label.text():
                return  # Файл уже есть в списке

        # Создаем новый элемент списка
        item = QListWidgetItem()
        h_layout = QHBoxLayout()

        # Название файла
        label = QLabel(f"Заказ №{order_info['order_number']}: {file_name}")
        label.setStyleSheet('color: red')
        label.setFont(QFont("Arial", 12))
        h_layout.addWidget(label)

        # Кнопка "Готов к выдаче"
        button = QPushButton('Готов к выдаче')
        button.clicked.connect(lambda: self.move_to_ready(file_name, item))
        button.setFixedSize(180, 30)
        h_layout.addWidget(button)

        # Кнопка "Подробная информация"
        info_button = QPushButton('Подробная информация')
        info_button.clicked.connect(lambda: self.show_file_info(file_name))
        info_button.setFixedSize(220, 30)
        h_layout.addWidget(info_button)

        # Виджет для элемента списка
        widget = QWidget()
        widget.setLayout(h_layout)
        widget.setMinimumHeight(50)
        widget.setMaximumHeight(50)

        # Добавляем элемент в список
        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)
        self.received_list.setSpacing(10)

    def show_file_info(self, file_name):
        """
        Отображает подробную информацию о файле.
        """
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        info_text = "\n".join([
            f"Имя файла: {file_name}",
            f"Цвет печати: {order_info.get('color', 'Неизвестно')}",
            f"Количество страниц: {order_info.get('pages', 'Неизвестно')}",
            f"Стоимость: {order_info.get('cost', 'Неизвестно')} руб.",
            f"Проверочный код: {order_info.get('check_number', 'Неизвестно')}",
            f"Комментарий: {order_info.get('comment', 'Нет комментария')}"
        ])
        QMessageBox.information(self, "Подробная информация о файле", info_text)

    def move_to_ready(self, file_name, item):
        """
        Перемещает файл в список готовых к выдаче.
        """
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if order_info:
            bot.send_message(order_info['user_id'], f"Ваш заказ изготовлен. Можете его забрать.")

        # Удаляем элемент из списка полученных
        row = self.received_list.row(item)
        self.received_list.takeItem(row)

        # Создаем новый элемент для списка готовых
        ready_item = QListWidgetItem()
        h_layout = QHBoxLayout()

        # Название файла
        label = QLabel(f"Заказ №{order_info['order_number']} готов к выдаче: {file_name}")
        label.setStyleSheet('color: green')
        label.setFont(QFont("Arial", 12))
        h_layout.addWidget(label)

        # Кнопка "Печать"
        print_button = QPushButton('Печать')
        print_button.clicked.connect(lambda: self.print_file(file_name))
        print_button.setFixedSize(100, 30)
        h_layout.addWidget(print_button)

        # Кнопка "Показать код выдачи"
        show_code_button = QPushButton('Показать код выдачи')
        show_code_button.clicked.connect(lambda: self.show_code(file_name))
        show_code_button.setFixedSize(185, 30)
        h_layout.addWidget(show_code_button)

        # Виджет для элемента списка
        ready_widget = QWidget()
        ready_widget.setLayout(h_layout)
        ready_widget.setMinimumHeight(50)
        ready_widget.setMaximumHeight(50)

        # Добавляем элемент в список готовых
        self.ready_list.insertItem(0, ready_item)
        self.ready_list.setItemWidget(ready_item, ready_widget)
        self.ready_list.setSpacing(10)

    def show_code(self, file_name):
        """
        Отображает код заказа и удаляет файл после подтверждения.
        """
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if not order_info:
            return

        msg_box = QMessageBox()
        msg_box.setWindowTitle("Код заказа")
        msg_box.setText(f"Код данного заказа: {order_info['check_number']}")
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

        if msg_box.exec_() == QMessageBox.Ok:
            file_path = os.path.join(os.getcwd(), file_name)
            try:
                os.remove(file_path)
                QMessageBox.information(self, "Успех", f"Файл {file_name} был успешно удален.")
                bot.send_message(order_info['user_id'], "Заказ должен быть у вас. Спасибо, что воспользовались нашими услугами!")
            except Exception as e:
                logging.error(f"Не удалось удалить файл: {str(e)}")

    def print_file(self, file_name):
        """
        Открывает файл для печати.
        """
        file_path = os.path.join(os.getcwd(), file_name)
        if not os.path.isfile(file_path):
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Файл не найден: {file_path}")
            return

        try:
            if file_name.lower().endswith('.pdf'):
                subprocess.run(["C:\\Users\\petry\\AppData\\Local\\Yandex\\YandexBrowser\\Application\\browser.exe", "/p", file_path])
            elif file_name.lower().endswith(('.docx', '.doc')):
                subprocess.run(["C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE", "/p", file_path])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось напечатать файл: {str(e)}")

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    receiver = FileReceiverApp()
    receiver.show()
    sys.exit(app.exec_())