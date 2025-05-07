import sys
import os
import logging
import hashlib
import aiohttp
import asyncio
import json
import requests
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
    QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout,
    QSpacerItem, QSizePolicy
)
from PyQt6.QtGui import QIcon
import websockets
import qasync
from qasync import asyncSlot, QEventLoop

API_URL = "http://localhost:5000"
WS_URL = "ws://localhost:5000/ws"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)


class WebSocketClient(QThread):
    update_received = pyqtSignal(list)

    def __init__(self, shop_id):
        super().__init__()
        self.shop_id = shop_id
        self.running = True

    async def run_async(self):
        async with websockets.connect(f"{WS_URL}/{self.shop_id}") as ws:
            while self.running:
                try:
                    data = await ws.recv()
                    # Явное преобразование JSON
                    self.update_received.emit(json.loads(data))
                except Exception as e:
                    logging.error(f"WebSocket error: {str(e)}")

    def run(self):
        asyncio.run(self.run_async())

    def stop(self):
        self.running = False


class FileReceiverApp(QWidget):
    def __init__(self, shop_id):
        super().__init__()
        self.shop_id = shop_id
        self.file_cache = set()
        self.current_items = {}
        self.ws_client = WebSocketClient(shop_id)
        self.ws_client.update_received.connect(self.handle_ws_update)
        self.init_ui()
        self.setup_timers()
        self.ws_client.start()

    def init_ui(self):
        self.setWindowIcon(QIcon("logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setMinimumSize(800, 600)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        control_layout = QHBoxLayout()
        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        control_layout.addWidget(self.refresh_btn)
        control_layout.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        self.received_list = QListWidget()
        self.ready_list = QListWidget()

        for lst in [self.received_list, self.ready_list]:
            lst.setStyleSheet("""
                QListWidget { 
                    background-color: #ffffff; 
                    border: 1px solid #cccccc; 
                    border-radius: 4px;
                }
                QListWidget::item { 
                    border-bottom: 1px solid #eeeeee; 
                }
            """)

        main_layout.addLayout(control_layout)
        main_layout.addWidget(QLabel('Полученные файлы:'))
        main_layout.addWidget(self.received_list)
        main_layout.addWidget(QLabel('Готовые к выдаче:'))
        main_layout.addWidget(self.ready_list)

        self.setLayout(main_layout)

    @asyncSlot()
    async def on_refresh_clicked(self):
        await self.load_orders()

    def setup_timers(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_timer_timeout)
        self.timer.start(30000)
        self.on_timer_timeout()

    @asyncSlot()
    async def on_timer_timeout(self):
        await self.load_orders()

    async def load_orders(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{API_URL}/orders",
                        params={'status': ['получен', 'готов'], 'shop_id': self.shop_id},
                        timeout=10
                ) as resp:
                    if resp.status == 200:
                        orders = await resp.json()
                        print(orders)
                        unique_orders = {order['ID']: order for order in orders}.values()
                        self.handle_orders(list(unique_orders))
        except Exception as e:
            self.show_error(f"Ошибка запроса: {str(e)}")

    def handle_ws_update(self, orders):
        self.handle_orders(orders)

    def handle_orders(self, orders):
        self.received_list.clear()
        self.ready_list.clear()
        self.current_items.clear()

        for order in orders:
            try:
                if not self.validate_order(order):
                    continue

                item = QListWidgetItem()
                widget = self.create_order_widget(order)

                if order['status'] == 'получен':
                    self.received_list.addItem(item)
                else:
                    self.ready_list.addItem(item)

                item.setSizeHint(widget.sizeHint())
                target_list = self.received_list if order['status'] == 'получен' else self.ready_list
                target_list.setItemWidget(item, widget)
                self.current_items[order['ID']] = (item, widget)

                if order['status'] == 'получен' and order['file_path'] not in self.file_cache:
                    asyncio.create_task(self.download_file(order['file_path']))

            except Exception as e:
                logging.error(f"Ошибка обработки заказа: {traceback.format_exc()}")

    def validate_order(self, order):
        required_fields = ['ID', 'status', 'file_path']
        return all(field in order for field in required_fields)

    def create_order_widget(self, order):
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(10)

        file_name = os.path.basename(order['file_path'])
        label_text = f"Заказ №{order['ID']}: {file_name}"
        label = QLabel(label_text)
        label.setWordWrap(True)
        color = "#dc3545" if order['status'] == 'получен' else "#28a745"
        label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: 600; font-size: 13px; padding: 5px; }}")

        buttons = []
        if order['status'] == 'получен':
            btn_print = QPushButton("Печать")
            btn_print.clicked.connect(lambda: self.print_file(order))
            btn_ready = QPushButton("Готово")
            btn_ready.clicked.connect(lambda: self.update_status(order['ID'], 'готов'))
            buttons = [btn_print, btn_ready]
        else:
            btn_complete = QPushButton("Выдать")
            btn_complete.clicked.connect(lambda: self.update_status(order['ID'], 'выдан'))
            buttons = [btn_complete]

        button_style = """
            QPushButton { 
                background-color: #f8f9fa; 
                border: 1px solid #dee2e6; 
                border-radius: 4px; 
                padding: 6px 12px;
            }
            QPushButton:hover { 
                background-color: #e2e6ea; 
            }
        """
        for btn in buttons:
            btn.setStyleSheet(button_style)
            layout.addWidget(btn)

        layout.addWidget(label)
        widget.setLayout(layout)
        return widget

    async def download_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.exists(filepath):
                return

            async with aiohttp.ClientSession() as session:
                async with session.get(f"{API_URL}/uploads/{filename}") as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        self.file_cache.add(filename)
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")

    def print_file(self, order):
        filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
        if sys.platform == "win32":
            os.startfile(filepath)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", filepath])

    @asyncSlot()
    async def update_status(self, order_id, new_status):
        try:
            endpoint = "ready" if new_status == "готов" else "complete"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{API_URL}/orders/{order_id}/{endpoint}"
                ) as resp:
                    if resp.status != 200:
                        self.show_error(f"Ошибка: {await resp.text()}")
        except Exception as e:
            self.show_error(f"Ошибка обновления: {str(e)}")

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        self.ws_client.stop()
        super().closeEvent(event)


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.shop_id = None
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle('Авторизация')
        self.setWindowIcon(QIcon("logo.png"))
        self.setFixedSize(320, 160)

        layout = QFormLayout(self)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Введите пароль магазина")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.authenticate)
        buttons.rejected.connect(self.reject)

        layout.addRow("Пароль:", self.password_input)
        layout.addRow(buttons)

    def authenticate(self):
        password = self.password_input.text().strip()
        if not password:
            QMessageBox.warning(self, "Ошибка", "Введите пароль")
            return

        try:
            hashed = hashlib.sha256(password.encode()).hexdigest()
            response = requests.get(
                f"{API_URL}/shop/{hashed}",
                timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                if "ID_shop" not in data:
                    raise ValueError("Некорректный ответ сервера")
                self.shop_id = data["ID_shop"]
                self.accept()
            else:
                QMessageBox.critical(self, "Ошибка", "Неверный пароль")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))


if __name__ == '__main__':
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        login_dialog = LoginDialog()
        if login_dialog.exec() == QDialog.DialogCode.Accepted and login_dialog.shop_id:
            window = FileReceiverApp(login_dialog.shop_id)
            window.show()
            loop.run_forever()