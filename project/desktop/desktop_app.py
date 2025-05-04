import sys
import os
import logging
import hashlib
import aiohttp
import asyncio
import qasync
import aiofiles
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
    QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout,
    QSpacerItem, QSizePolicy
)
from PyQt5.QtGui import QIcon

API_URL = "http://localhost:5000"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)


class AsyncWorker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, coro):
        super().__init__()
        self.coro = coro

    async def run(self):
        try:
            result = await self.coro
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class LoginDialog(QDialog):
    def accept(self):
        if self.session:
            asyncio.create_task(self.session.close())
        super().accept()

    def __init__(self):
        super().__init__()
        self.session = None
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle('Авторизация')
        self.setFixedSize(300, 150)
        layout = QFormLayout(self)

        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.start_auth)
        buttons.rejected.connect(self.reject)

        layout.addRow("Пароль:", self.password_input)
        layout.addRow(buttons)

    def start_auth(self):
        self.session = aiohttp.ClientSession()
        worker = AsyncWorker(self.authenticate())
        worker.finished.connect(self.accept)
        worker.error.connect(self.handle_error)
        asyncio.create_task(worker.run())

    async def authenticate(self):
        password = self.password_input.text()
        if not password:
            raise ValueError("Введите пароль")

        hashed = hashlib.sha256(password.encode()).hexdigest()
        async with self.session.get(f"{API_URL}/shop/{hashed}", timeout=5) as resp:
            if resp.status != 200:
                raise PermissionError("Неверный пароль")

    def handle_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)
        logging.error(f"AUTH ERROR: {message}")
        if self.session:
            asyncio.create_task(self.session.close())

    def closeEvent(self, event):
        if self.session:
            asyncio.create_task(self.session.close())
        super().closeEvent(event)


class FileReceiverApp(QWidget):
    def __init__(self):
        super().__init__()
        self.session = aiohttp.ClientSession()
        self.file_cache = set()
        self.init_ui()
        self.setup_timers()
        sys.excepthook = self.handle_exception

    def handle_exception(self, exc_type, exc_value, exc_traceback):
        logging.error("Неперехваченное исключение", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(self, "Ошибка", str(exc_value))

    def setup_timers(self):
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: self.execute_async(self.load_orders()))
        self.timer.start(300000)
        self.execute_async(self.load_orders())

    def init_ui(self):
        self.setWindowIcon(QIcon("logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        main_layout = QVBoxLayout()
        control_layout = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(lambda: self.execute_async(self.load_orders()))

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

    def execute_async(self, coro):
        worker = AsyncWorker(coro)
        worker.finished.connect(self.handle_response)
        worker.error.connect(self.show_error)
        asyncio.create_task(worker.run())

    def closeEvent(self, event):
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())
        super().closeEvent(event)

    async def load_orders(self):
        try:
            async with self.session.get(
                    f"{API_URL}/orders",
                    params={'status': ['получен', 'готов']},
                    timeout=15  # Увеличено время ожидания
            ) as resp:
                if resp.status == 200:
                    orders = await resp.json()
                    logging.debug(f"Получено заказов: {len(orders)}")
                    return orders
                logging.error(f"Ошибка HTTP: {resp.status}")
                return []
        except Exception as e:
            logging.error(f"Ошибка запроса: {str(e)}")
            return None

    def handle_response(self, orders):
        try:
            if orders:
                self.update_lists(orders)
            else:
                self.show_error("Не удалось загрузить заказы")
        except Exception as e:
            logging.error(f"Ошибка загрузки заказов: {str(e)}")


    def update_lists(self, orders):
        if not orders:
            logging.warning("Пустой список заказов")
            return

        try:
            self.clear_lists()
            valid_orders = sorted(
                [o for o in orders if o.get('status', '').strip().lower() in {'получен', 'готов'}],
                key=lambda x: x['ID'],
                reverse=True
            )

            for order in valid_orders:
                status = order['status'].strip().lower()
                if status == 'получен':
                    self.add_received_order(order)
                elif status == 'готов':
                    self.add_ready_order(order)

        except KeyError as e:
            logging.error(f"Ключ отсутствует: {str(e)}")
            self.show_error("Ошибка формата данных")
        except Exception as e:
            logging.error(f"Критическая ошибка: {str(e)}")
            self.show_error("Ошибка обработки данных")

    def clear_lists(self):
        self.received_list.clear()
        self.ready_list.clear()
        self.file_cache.clear()

    def add_received_order(self, order):
        item = QListWidgetItem()
        widget = self.create_order_widget(
            order,
            "red",
            [("Печать", lambda: self.execute_async(self.print_file(order))),
             ("Готово", lambda: self.execute_async(self.update_status(order['ID'], 'готов')))]
        )
        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)
        self.execute_async(self.download_file(order['file_path']))

    def add_ready_order(self, order):
        item = QListWidgetItem()
        widget = self.create_order_widget(
            order,
            "green",
            [("Выдать", lambda: self.execute_async(self.update_status(order['ID'], 'выдан')))]
        )
        self.ready_list.addItem(item)
        self.ready_list.setItemWidget(item, widget)

    def create_order_widget(self, order, color, buttons):
        widget = QWidget()
        layout = QHBoxLayout()
        label = QLabel(f"Заказ №{order['ID']}: {order['file_path']}")
        label.setStyleSheet(f"color: {color}; font-weight: bold;")
        layout.addWidget(label)

        for btn_text, callback in buttons:
            btn = QPushButton(btn_text)
            btn.clicked.connect(callback)
            layout.addWidget(btn)

        widget.setLayout(layout)
        return widget

    async def download_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if filename in self.file_cache or os.path.exists(filepath):
                return

            async with self.session.get(f"{API_URL}/files/{filename}", timeout=10) as resp:
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        await f.write(chunk)
            self.file_cache.add(filename)
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")

    async def print_file(self, order):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
            if not os.path.exists(filepath):
                raise FileNotFoundError("Файл не найден")

            if sys.platform == "win32":
                os.startfile(filepath)
            else:
                import subprocess
                subprocess.Popen([filepath], shell=True)
        except Exception as e:
            self.show_error(f"Ошибка печати: {str(e)}")

    async def update_status(self, order_id, new_status):
        try:
            async with self.session.post(
                    f"{API_URL}/order",
                    params={'id': order_id},
                    json={'status': new_status},
                    timeout=10
            ) as resp:
                if resp.status != 200:
                    raise ConnectionError(await resp.text())
                await self.load_orders()
        except Exception as e:
            self.show_error(str(e))

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        asyncio.create_task(self.session.close())
        super().closeEvent(event)


async def main():
    login = LoginDialog()
    login.show()

    future = asyncio.Future()
    login.finished.connect(future.set_result)
    await future

    if login.result() == QDialog.Accepted:
        window = FileReceiverApp()
        window.show()
        return window
    return None


if __name__ == '__main__':
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    try:
        with loop:
            main_window = loop.run_until_complete(main())
            if main_window:
                loop.run_forever()
            else:
                loop.stop()
    except Exception as e:
        logging.critical(f"ФАТАЛЬНАЯ ОШИБКА: {str(e)}")
    finally:
        loop.close()
    sys.exit(0)