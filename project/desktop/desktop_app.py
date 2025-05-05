import sys
import os
import logging
import hashlib
import traceback
import requests
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer, QMutex
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
    QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout,
    QSpacerItem, QSizePolicy
)
from PyQt6.QtGui import QIcon

API_URL = "http://localhost:5000"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)


class TaskManager(QObject):
    tasks = set()
    lock = QMutex()


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.shop_id = None
        self.session = requests.Session()
        self.setup_ui()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def setup_ui(self):
        self.setWindowTitle('Авторизация')
        self.setWindowIcon(QIcon("logo.png"))
        self.setFixedSize(300, 150)
        layout = QFormLayout(self)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.authenticate)
        buttons.rejected.connect(self.reject)

        layout.addRow("Пароль:", self.password_input)
        layout.addRow(buttons)

    def authenticate(self):
        try:
            password = self.password_input.text()
            if not password:
                raise ValueError("Введите пароль")

            hashed = hashlib.sha256(password.encode()).hexdigest()
            resp = self.session.get(f"{API_URL}/shop/{hashed}")

            if resp.status_code != 200:
                raise PermissionError("Неверный пароль")

            data = resp.json()
            self.shop_id = data["ID_shop"]
            self.accept()

        except Exception as e:
            self.handle_error(str(e))
            self.reject()

    def handle_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)
        logging.error(f"AUTH ERROR: {message}")
        self.session.close()

    def closeEvent(self, event):
        self.session.close()
        super().closeEvent(event)


class FileReceiverApp(QWidget):
    def __init__(self, session, shop_id):
        super().__init__()
        self.session = session
        self.shop_id = shop_id
        self.file_cache = set()
        self.current_items = {}
        self.init_ui()
        self.setup_timers()
        sys.excepthook = self.handle_exception

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        logging.error("Неперехваченное исключение", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(self, "Ошибка", str(exc_value))

    def setup_timers(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.safe_load_orders)  # Используем "безопасный" метод
        self.timer.start(5000)  # 5 секунд

    def safe_load_orders(self):
        if not self.isVisible():  # Не обновляем, если окно свёрнуто/закрыто
            return
        self.load_orders()

    def init_ui(self):
        self.setWindowIcon(QIcon("logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        main_layout = QVBoxLayout()
        control_layout = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.load_orders)

        control_layout.addWidget(self.refresh_btn)
        control_layout.addItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        self.received_list = QListWidget()
        self.ready_list = QListWidget()

        main_layout.addLayout(control_layout)
        main_layout.addWidget(QLabel('Полученные файлы:'))
        main_layout.addWidget(self.received_list)
        main_layout.addWidget(QLabel('Готовые к выдаче:'))
        main_layout.addWidget(self.ready_list)

        self.setLayout(main_layout)

    def load_orders(self):
        try:
            print(f"Loading orders for shop_id: {self.shop_id}")  # Debug
            resp = self.session.get(
                f"{API_URL}/orders",
                params={'status': ['получен', 'готов'], 'shop_id': self.shop_id},
                timeout=15
            )
            print(f"Response status: {resp.status_code}")  # Debug
            print(f"Response content: {resp.text}")  # Debug

            if resp.status_code == 200:
                orders = resp.json()
                print(f"Received orders: {orders}")  # Debug
                self.handle_response(orders)
        except Exception as e:
            logging.error(f"Ошибка запроса: {traceback.format_exc()}")

    def handle_response(self, orders):
        if not orders:
            print("Received empty orders list, preserving current items")
            return

        print(f"Processing {len(orders)} orders")

        current_ids = set(self.current_items.keys())
        new_ids = set()

        for order in orders:
            if 'ID' not in order or 'status' not in order:
                continue

            order_id = order['ID']
            new_ids.add(order_id)

            # Всегда обновляем существующие виджеты
            if order_id in current_ids:
                print(f"Updating existing order: {order_id}")
                self._update_existing_widget(order)
            else:
                print(f"Adding new order: {order_id}")
                self.add_order_widget(order)

        # Удаляем только те заказы, которых больше нет в ответе
        for order_id in current_ids - new_ids:
            print(f"Removing old order: {order_id}")
            self.remove_order_widget(order_id)

    def add_order_widget(self, order):
        QTimer.singleShot(0, lambda: self._safe_add_widget(order))

    def _update_existing_widget(self, order):
        if order['ID'] not in self.current_items:
            return

        item, widget = self.current_items[order['ID']]
        if not widget or not item:
            return

        try:
            new_status = order['status'].lower().strip()
            current_list = item.listWidget()
            target_list = self.received_list if new_status == 'получен' else self.ready_list

            # Получаем текущий статус из виджета
            current_status = 'получен' if current_list == self.received_list else 'готов'

            # Если статус изменился, перемещаем в другой список
            if current_status != new_status:
                print(f"Moving order {order['ID']} from {current_status} to {new_status}")

                # Удаляем из текущего списка
                current_list.takeItem(current_list.row(item))

                # Создаем новый виджет (старый удалится автоматически)
                new_widget = QWidget()
                new_widget.setMinimumSize(400, 60)
                layout = QHBoxLayout()
                layout.setContentsMargins(10, 5, 10, 5)
                new_widget.setLayout(layout)

                # Добавляем информацию о заказе
                file_name = os.path.basename(order['file_path'])
                label_text = f"Заказ №{order['ID']}: {file_name}"
                label = QLabel(label_text)
                label.setStyleSheet("""
                    font-weight: bold;
                    font-size: 14px;
                    padding: 5px;
                """)
                layout.addWidget(label, stretch=1)

                # Добавляем кнопки
                buttons = []
                if new_status == 'получен':
                    buttons = [
                        ("Печать", lambda: self.print_file(order)),
                        ("Готово", lambda: self.update_status(order['ID'], 'готов'))
                    ]
                else:
                    buttons = [
                        ("Выдать", lambda: self.update_status(order['ID'], 'выдан'))  # Чётко указываем статус
                    ]

                for text, callback in buttons:
                    btn = QPushButton(text)
                    btn.setStyleSheet("""
                        QPushButton {
                            padding: 5px 10px;
                            min-width: 90px;
                            font-size: 12px;
                        }
                    """)
                    btn.clicked.connect(callback)
                    layout.addWidget(btn)

                # Добавляем в целевой список
                target_list.addItem(item)
                target_list.setItemWidget(item, new_widget)

                # Обновляем ссылку на виджет
                self.current_items[order['ID']] = (item, new_widget)
            else:
                # Если статус не изменился, просто обновляем кнопки
                layout = widget.layout()
                for i in reversed(range(layout.count())):
                    child = layout.itemAt(i).widget()
                    if child and not isinstance(child, QLabel):
                        child.deleteLater()

                # Добавляем новые кнопки
                buttons = []
                if new_status == 'получен':
                    buttons = [
                        ("Печать", lambda: self.print_file(order)),
                        ("Готово", lambda: self.update_status(order['ID'], 'готов'))
                    ]
                else:
                    buttons = [
                        ("Выдать", lambda: self.update_status(order['ID'], 'выдан'))
                    ]

                for text, callback in buttons:
                    btn = QPushButton(text)
                    btn.setStyleSheet("""
                        QPushButton {
                            padding: 5px 10px;
                            min-width: 90px;
                            font-size: 12px;
                        }
                    """)
                    btn.clicked.connect(callback)
                    layout.addWidget(btn)

        except Exception as e:
            logging.error(f"Ошибка обновления виджета: {traceback.format_exc()}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось обновить заказ: {str(e)}")

    def _safe_add_widget(self, order):
        # Проверяем, не существует ли уже виджет для этого заказа
        if order['ID'] in self.current_items:
            print(f"Widget for order {order['ID']} already exists, skipping creation")
            return
        try:
            print(f"Creating widget for order {order}")  # Выводим весь заказ для отладки

            # Проверка обязательных полей с более информативным сообщением
            required_fields = ['ID', 'status', 'file_path']
            missing_fields = [field for field in required_fields if field not in order]
            if missing_fields:
                error_msg = f"Missing required fields: {', '.join(missing_fields)} in order {order.get('ID', 'unknown')}"
                print(error_msg)
                logging.error(error_msg)
                return

            status = order['status'].lower().strip()
            if status not in {'получен', 'готов'}:
                error_msg = f"Invalid status: {status} for order {order['ID']}"
                print(error_msg)
                logging.error(error_msg)
                return

            # Создаем виджет
            widget = QWidget()
            widget.setMinimumSize(400, 60)  # Увеличиваем минимальный размер

            layout = QHBoxLayout()
            layout.setContentsMargins(10, 5, 10, 5)

            # Информация о заказе
            file_name = os.path.basename(order['file_path'])
            label_text = f"Заказ №{order['ID']}: {file_name}"
            label = QLabel(label_text)
            label.setStyleSheet("""
                font-weight: bold;
                font-size: 14px;
                padding: 5px;
            """)
            layout.addWidget(label, stretch=1)

            # Кнопки действий
            buttons = []
            if status == 'получен':
                buttons = [
                    ("Печать", lambda: self.print_file(order)),
                    ("Готово", lambda: self.update_status(order['ID'], 'готов'))
                ]
            else:
                buttons = [
                    ("Выдать", lambda: self.update_status(order['ID'], 'выдан'))
                ]

            for text, callback in buttons:
                btn = QPushButton(text)
                btn.setStyleSheet("""
                    QPushButton {
                        padding: 5px 10px;
                        min-width: 90px;
                        font-size: 12px;
                    }
                """)
                btn.clicked.connect(callback)
                layout.addWidget(btn)

            widget.setLayout(layout)

            # Создаем элемент списка
            item = QListWidgetItem()
            item.setSizeHint(widget.sizeHint())

            # Добавляем в соответствующий список
            target_list = self.received_list if status == 'получен' else self.ready_list
            target_list.addItem(item)
            target_list.setItemWidget(item, widget)

            # Сохраняем ссылку
            self.current_items[order['ID']] = (item, widget)

            print(f"Successfully created widget for order {order['ID']}")  # Подтверждение создания

            # Загружаем файл если нужно
            if status == 'получен' and order['file_path'] not in self.file_cache:
                self.download_file(order['file_path'])

        except Exception as e:
            error_msg = f"Error creating widget for order {order.get('ID', 'unknown')}: {str(e)}"
            print(error_msg)
            logging.error(error_msg + "\n" + traceback.format_exc())
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать элемент для заказа: {str(e)}")

    def create_order_widget(self, order, color, buttons):
        try:
            widget = QWidget()
            layout = QHBoxLayout()

            file_name = os.path.basename(order['file_path'])
            label_text = f"Заказ №{order['ID']}: {file_name}"
            label = QLabel(label_text)
            label.setStyleSheet(f"color: {color}; font-weight: bold;")
            layout.addWidget(label)

            for btn_text, callback in buttons:
                btn = QPushButton(btn_text)
                btn.clicked.connect(callback)
                layout.addWidget(btn)

            widget.setLayout(layout)
            return widget

        except Exception as e:
            logging.error(f"Ошибка создания виджета: {traceback.format_exc()}")
            return None

    def remove_order_widget(self, order_id):
        QTimer.singleShot(0, lambda: self._safe_remove_widget(order_id))

    def _safe_remove_widget(self, order_id):
        if order_id in self.current_items:
            item, widget = self.current_items.pop(order_id)
            list_widget = item.listWidget()
            if list_widget:
                list_widget.takeItem(list_widget.row(item))

    def closeEvent(self, event):
        TaskManager.lock.lock()
        try:
            for task in TaskManager.tasks:
                if hasattr(task, 'cancel'):
                    task.cancel()
        finally:
            TaskManager.lock.unlock()

        if self.session:
            self.session.close()
        super().closeEvent(event)

    def download_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if filename in self.file_cache:
                return

            resp = self.session.get(f"{API_URL}/files/{filename}", timeout=10, stream=True)
            if resp.status_code == 200:
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                self.file_cache.add(filename)
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {traceback.format_exc()}")

    def print_file(self, order):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
            if sys.platform == "win32":
                os.startfile(filepath)
            else:
                import subprocess
                subprocess.Popen([filepath], shell=True)
        except Exception as e:
            self.show_error(f"Ошибка печати: {traceback.format_exc()}")

    def update_status(self, order_id, new_status):
        try:
            resp = self.session.post(
                f"{API_URL}/orders/{order_id}/complete",
                json={'status': new_status},  # Отправляем выбранный статус
                timeout=10
            )
            if resp.status_code != 200:
                raise ConnectionError(resp.text)

            # Ждём 1 секунду перед обновлением, чтобы сервер успел обработать запрос
            QTimer.singleShot(1000, self.load_orders)

        except Exception as e:
            self.show_error(f"Ошибка обновления: {traceback.format_exc()}")

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)


if __name__ == '__main__':
    app = QApplication(sys.argv)

    login_dialog = LoginDialog()
    result = login_dialog.exec()

    if result == QDialog.DialogCode.Accepted and login_dialog.shop_id:
        main_window = FileReceiverApp(login_dialog.session, login_dialog.shop_id)
        main_window.show()
        sys.exit(app.exec())
    else:
        app.quit()