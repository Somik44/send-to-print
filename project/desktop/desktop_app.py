import sys
import os
import requests
import logging
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer, QObject
import hashlib
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
                            QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
                            QLineEdit, QDialog, QDialogButtonBox, QFormLayout, QListWidgetItem, QSpacerItem, QSizePolicy)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QIcon, QFont

API_URL = "http://localhost:5000"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)

class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Авторизация')
        self.setFixedSize(300, 150)

        layout = QFormLayout(self)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.verify_password)
        buttons.rejected.connect(self.reject)

        layout.addRow("Пароль:", self.password_input)
        layout.addRow(buttons)

    def verify_password(self):
        password = self.password_input.text()
        if not password:
            QMessageBox.warning(self, "Ошибка", "Введите пароль")
            return

        try:
            # Хэшируем введенный пароль
            hashed_password = hashlib.sha256(password.encode()).hexdigest()

            # Проверяем доступность сервера
            try:
                response = requests.get(f"{API_URL}/api/shop/password", timeout=5)
                if not response.ok:
                    raise ConnectionError("Сервер недоступен")
            except:
                raise ConnectionError("Не удалось подключиться к серверу")

            # Получаем список всех паролей из БД
            try:
                response = requests.get(f"{API_URL}/api/shop/password", timeout=10)
                if response.ok:
                    shop_passwords = response.json()
                    if isinstance(shop_passwords, dict) and shop_passwords.get('status') == 'error':
                        raise ValueError(shop_passwords.get('message', 'Ошибка сервера'))

                    # Проверяем, есть ли совпадение хэшей
                    if any(hashed_password == shop_pwd for shop_pwd in shop_passwords):
                        self.accept()
                    else:
                        QMessageBox.critical(self, "Ошибка", "Неверный пароль")
                else:
                    QMessageBox.critical(self, "Ошибка", f"Ошибка сервера: {response.status_code}")
            except ValueError as ve:
                QMessageBox.critical(self, "Ошибка", f"Ошибка данных: {str(ve)}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Ошибка запроса: {str(e)}")

        except ConnectionError as ce:
            QMessageBox.critical(self, "Ошибка подключения", f"Не удалось подключиться к серверу: {str(ce)}")
        except Exception as e:
            logging.error(f"Ошибка проверки пароля: {str(e)}")
            QMessageBox.critical(self, "Ошибка", "Произошла непредвиденная ошибка")

class OrderLoader(QThread):
    data_loaded = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def run(self):
        try:
            response = requests.get(
                f"{API_URL}/orders",
                params={'status[]': ['получен', 'готов']},
                timeout=10
            )
            if response.status_code == 200:
                self.data_loaded.emit(response.json())
            else:
                self.error_occurred.emit(f"HTTP Error {response.status_code}")
        except Exception as e:
            self.error_occurred.emit(f"Ошибка соединения: {str(e)}")

class OrderUpdater(QObject):
    finished = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def update_status(self, order_id, new_status):
        try:
            response = requests.post(
                f"{API_URL}/order?id={order_id}",
                json={'status': new_status},
                timeout=10
            )
            if response.status_code == 200:
                self.finished.emit(True)
            else:
                self.error_occurred.emit(response.text)
        except Exception as e:
            self.error_occurred.emit(str(e))

class FileReceiverApp(QWidget):
    def __init__(self):
        super().__init__()
        self.threads = []
        self.initUI()
        self.setup_timers()
        sys.excepthook = self.handle_exception

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        logging.error("Неперехваченное исключение", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(self, "Ошибка", str(exc_value))

    def setup_timers(self):
        self.auto_refresh_timer = QTimer()
        self.auto_refresh_timer.timeout.connect(self.load_orders)
        self.auto_refresh_timer.start(300000)
        self.load_orders()

    def initUI(self):
        self.setWindowIcon(QIcon("logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        main_layout = QVBoxLayout()
        control_layout = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.load_orders)

        control_layout.addWidget(self.refresh_btn)
        control_layout.addItem(QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))

        self.received_list = QListWidget()
        self.ready_list = QListWidget()

        main_layout.addLayout(control_layout)
        main_layout.addWidget(QLabel('Полученные файлы:'))
        main_layout.addWidget(self.received_list)
        main_layout.addWidget(QLabel('Готовые к выдаче:'))
        main_layout.addWidget(self.ready_list)

        self.setLayout(main_layout)

    def load_orders(self):
        self.loader = OrderLoader()
        self.loader.data_loaded.connect(self.update_lists)
        self.loader.error_occurred.connect(self.show_error)
        self.loader.start()

    def update_lists(self, orders):
        self.received_list.clear()
        self.ready_list.clear()

        for order in orders:
            if order['status'] == 'получен':
                self.add_received_order(order)
            elif order['status'] == 'готов':
                self.add_ready_order(order)

    def add_received_order(self, order):
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout()

        label = QLabel(f"Заказ №{order['ID']}: {order['file_path']}")
        label.setStyleSheet("color: red;")

        btn_print = QPushButton("Печать")
        btn_print.clicked.connect(lambda: self.print_file(order))

        btn_ready = QPushButton("Готово")
        btn_ready.clicked.connect(lambda: self.update_status(order['ID'], 'готов'))

        layout.addWidget(label)
        layout.addWidget(btn_print)
        layout.addWidget(btn_ready)
        widget.setLayout(layout)

        item.setSizeHint(widget.sizeHint())
        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)

        self.download_file(order['file_path'])

    def add_ready_order(self, order):
        item = QListWidgetItem()
        widget = QWidget()
        layout = QHBoxLayout()

        label = QLabel(f"Заказ №{order['ID']}: {order['file_path']}")
        label.setStyleSheet("color: green;")

        btn_complete = QPushButton("Выдать")
        btn_complete.clicked.connect(lambda: self.update_status(order['ID'], 'выдан'))

        layout.addWidget(label)
        layout.addWidget(btn_complete)
        widget.setLayout(layout)

        item.setSizeHint(widget.sizeHint())
        self.ready_list.addItem(item)
        self.ready_list.setItemWidget(item, widget)

    def download_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if not os.path.exists(filepath):
                response = requests.get(f"{API_URL}/api/files/{filename}", stream=True)
                response.raise_for_status()
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(8192):
                        f.write(chunk)
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")

    def print_file(self, order):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
            if os.path.exists(filepath):
                os.startfile(filepath)
            else:
                self.show_error("Файл не найден")
        except Exception as e:
            self.show_error(f"Ошибка печати: {str(e)}")

    def update_status(self, order_id, new_status):
        updater = OrderUpdater()
        thread = QThread()
        updater.moveToThread(thread)
        thread.started.connect(lambda: updater.update_status(order_id, new_status))
        updater.finished.connect(thread.quit)
        updater.finished.connect(self.load_orders)
        updater.error_occurred.connect(self.show_error)
        thread.finished.connect(thread.deleteLater)
        self.threads.append(thread)
        thread.start()

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

if __name__ == '__main__':
    app = QApplication(sys.argv)

    # Показываем диалог входа
    login = LoginDialog()
    if login.exec_() == QDialog.Accepted:
        window = FileReceiverApp()
        window.show()
        sys.exit(app.exec_())
    else:
        sys.exit(0)