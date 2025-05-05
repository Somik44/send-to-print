import sys
import os
import logging
import hashlib
import aiohttp
import asyncio
import qasync
import aiofiles
import traceback
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

class AsyncWorker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, coro):
        super().__init__()
        self.coro = coro
        self.task = None
        self._is_cancelled = False

    async def run(self):
        TaskManager.lock.lock()
        try:
            TaskManager.tasks.add(self)
        finally:
            TaskManager.lock.unlock()

        try:
            result = await self.coro
            if not self._is_cancelled:
                self.finished.emit(result)
        except Exception as e:
            if not self._is_cancelled:
                self.error.emit(f"Ошибка: {traceback.format_exc()}")
        finally:
            TaskManager.lock.lock()
            try:
                TaskManager.tasks.discard(self)
            finally:
                TaskManager.lock.unlock()

    def cancel(self):
        self._is_cancelled = True
        if self.task:
            self.task.cancel()

class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.shop_id = None
        self.session = None
        self.setup_ui()
        self.setWindowModality(Qt.WindowModality.ApplicationModal)  # Блокирующий диалог
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # Автоудаление

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
        buttons.accepted.connect(self.start_auth)
        buttons.rejected.connect(self.reject)

        layout.addRow("Пароль:", self.password_input)
        layout.addRow(buttons)

    def start_auth(self):
        self.session = aiohttp.ClientSession()
        worker = AsyncWorker(self.authenticate())
        worker.finished.connect(self.accept)
        worker.error.connect(self.handle_error)
        worker.task = asyncio.create_task(worker.run())

    async def authenticate(self):
        try:
            password = self.password_input.text()
            if not password:
                raise ValueError("Введите пароль")

            hashed = hashlib.sha256(password.encode()).hexdigest()
            async with self.session.get(f"{API_URL}/shop/{hashed}") as resp:
                if resp.status != 200:
                    raise PermissionError("Неверный пароль")
                data = await resp.json()
                self.shop_id = data["ID_shop"]
            if resp.status != 200:
                self.reject()  # Явное закрытие с ошибкой
                return

            self.accept()  # Успешное закрытие

        except Exception as e:
            self.reject()
            raise

    def handle_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)
        logging.error(f"AUTH ERROR: {message}")
        asyncio.create_task(self.session.close())

    def closeEvent(self, event):
        if self.session:
            asyncio.create_task(self.session.close())
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
        self.timer.timeout.connect(lambda: self.execute_async(self.load_orders()))
        self.timer.start(30000)
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
        control_layout.addItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

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
        worker.task = asyncio.create_task(worker.run())

    async def load_orders(self):
        try:
            async with self.session.get(
                    f"{API_URL}/orders",
                    params={'status': ['получен', 'готов'], 'shop_id': self.shop_id},
                    timeout=15
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return []
        except Exception as e:
            logging.error(f"Ошибка запроса: {traceback.format_exc()}")
            return []

    def handle_response(self, orders):
        if not orders:
            return

        valid_orders = []
        for order in orders:
            if 'ID' not in order or 'status' not in order:
                logging.error(f"Некорректный заказ: {order}")
                continue
            valid_orders.append(order)

        new_orders = {o['ID']: o for o in valid_orders}
        current_ids = set(self.current_items.keys())

        for order_id in current_ids - new_orders.keys():
            self.remove_order_widget(order_id)

        for order_id, order in new_orders.items():
            if order_id not in current_ids:
                self.add_order_widget(order)
            else:
                self._update_existing_widget(order)

    def add_order_widget(self, order):
        QTimer.singleShot(0, lambda: self._safe_add_widget(order))

    def _update_existing_widget(self, order):
        item, widget = self.current_items.get(order['ID'], (None, None))
        if not widget:
            return

        try:
            label = widget.findChild(QLabel)
            if label:
                label.setText(f"Заказ №{order['ID']}: {order['file_path']}")

            layout = widget.layout()
            for i in reversed(range(layout.count())):
                layout.itemAt(i).widget().deleteLater()

            status = order['status'].lower().strip()
            buttons = []
            if status == 'получен':
                buttons = [
                    ("Печать", lambda _, o=order: self.execute_async(self.print_file(o))),
                    ("Готово", lambda _, o=order: self.execute_async(self.update_status(o['ID'], 'готов')))
                ]
            elif status == 'готов':
                buttons = [
                    ("Выдать", lambda _, o=order: self.execute_async(self.update_status(o['ID'], 'выдан')))
                ]

            for btn_text, callback in buttons:
                btn = QPushButton(btn_text)
                btn.clicked.connect(callback)
                layout.addWidget(btn)

        except Exception as e:
            logging.error(f"Ошибка обновления виджета: {traceback.format_exc()}")

    def _safe_add_widget(self, order):
        try:
            required_fields = ['ID', 'status', 'file_path']
            for field in required_fields:
                if field not in order:
                    logging.error(f"Отсутствует поле '{field}': {order}")
                    return

            status = order['status'].strip().lower()
            if status not in {'получен', 'готов'}:
                logging.warning(f"Неизвестный статус: {status}")
                return

            item = QListWidgetItem()
            color = "#FF0000" if status == 'получен' else "#00FF00"

            buttons = []
            if status == 'получен':
                buttons = [
                    ("Печать", lambda: self.execute_async(self.print_file(order))),
                    ("Готово", lambda: self.execute_async(self.update_status(order['ID'], 'готов')))
                ]
            else:
                buttons = [
                    ("Выдать", lambda: self.execute_async(self.update_status(order['ID'], 'выдан')))
                ]

            widget = self.create_order_widget(order, color, buttons)
            if not widget:
                return

            target_list = self.received_list if status == 'получен' else self.ready_list
            target_list.addItem(item)
            target_list.setItemWidget(item, widget)

            self.current_items[order['ID']] = (item, widget)

            if status == 'получен' and order['file_path'] not in self.file_cache:
                self.execute_async(self.download_file(order['file_path']))

        except Exception as e:
            logging.error(f"Ошибка создания виджета: {traceback.format_exc()}")

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
                task.cancel()
        finally:
            TaskManager.lock.unlock()

        if self.session:
            asyncio.create_task(self.session.close())
        super().closeEvent(event)

    async def download_file(self, filename):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if filename in self.file_cache:
                return

            async with self.session.get(f"{API_URL}/files/{filename}", timeout=10) as resp:
                if resp.status == 200:
                    async with aiofiles.open(filepath, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            await f.write(chunk)
                    self.file_cache.add(filename)
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {traceback.format_exc()}")

    async def print_file(self, order):
        try:
            filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
            if sys.platform == "win32":
                os.startfile(filepath)
            else:
                import subprocess
                subprocess.Popen([filepath], shell=True)
        except Exception as e:
            self.show_error(f"Ошибка печати: {traceback.format_exc()}")

    async def update_status(self, order_id, new_status):
        try:
            async with self.session.post(
                    f"{API_URL}/orders/{order_id}/complete",
                    json={'status': new_status},
                    timeout=10
            ) as resp:
                if resp.status != 200:
                    raise ConnectionError(await resp.text())
                await self.load_orders()
        except Exception as e:
            self.show_error(f"Ошибка обновления: {traceback.format_exc()}")

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)


async def main():
    try:
        # Создаем и настраиваем диалог авторизации
        login_dialog = LoginDialog()
        login_dialog.show()
        login_dialog.activateWindow()

        # Ждем завершения работы диалога
        result = await qasync.async_dialog_exec(login_dialog)

        # Диалог закрывается в любом случае
        login_dialog.close()

        # Проверяем результат авторизации
        if result == QDialog.DialogCode.Accepted and login_dialog.shop_id:
            # Создаем главное окно приложения
            main_window = FileReceiverApp(login_dialog.session, login_dialog.shop_id)
            main_window.show()
            main_window.activateWindow()
            return main_window

        # Выход если авторизация не прошла
        return None

    except Exception as e:
        logging.error(f"Main error: {traceback.format_exc()}")
        QMessageBox.critical(None, "Ошибка", f"Фатальная ошибка: {str(e)}")
        return None


if __name__ == '__main__':
    app = QApplication(sys.argv)

    # Настройка асинхронного цикла
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    try:
        with loop:
            # Запускаем главную корутину
            main_task = loop.create_task(main())

            # Завершаем приложение когда задача завершена
            main_task.add_done_callback(
                lambda t: app.quit() if t.result() is None else None
            )

            loop.run_forever()

    except Exception as e:
        logging.critical(f"Application crash: {traceback.format_exc()}")

    finally:
        loop.close()
    sys.exit(0)


# import sys
# import os
# import logging
# import hashlib
# import aiohttp
# import asyncio
# import qasync
# import aiofiles
# import traceback
# from PyQt5.QtCore import Qt, QObject, pyqtSignal, QTimer, QMutex
# from PyQt5.QtWidgets import (
#     QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
#     QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
#     QLineEdit, QDialog, QDialogButtonBox, QFormLayout,
#     QSpacerItem, QSizePolicy
# )
# from PyQt5.QtGui import QIcon
#
# API_URL = "http://localhost:5000"
# DOWNLOAD_DIR = os.path.abspath('downloads')
# os.makedirs(DOWNLOAD_DIR, exist_ok=True)
#
# logging.basicConfig(
#     level=logging.DEBUG,
#     format="%(asctime)s - %(levelname)s - %(message)s",
#     filename='desktop_app.log'
# )
#
#
# class TaskManager(QObject):
#     tasks = set()
#     lock = QMutex()
#
#
# class AsyncWorker(QObject):
#     finished = pyqtSignal(object)
#     error = pyqtSignal(str)
#
#     def __init__(self, coro):
#         super().__init__()
#         self.coro = coro
#         self.task = None
#         self._is_cancelled = False
#
#     async def run(self):
#         TaskManager.lock.lock()
#         try:
#             TaskManager.tasks.add(self)
#         finally:
#             TaskManager.lock.unlock()
#
#         try:
#             result = await self.coro
#             if not self._is_cancelled:
#                 self.finished.emit(result)
#         except Exception as e:
#             if not self._is_cancelled:
#                 self.error.emit(f"Ошибка: {traceback.format_exc()}")
#         finally:
#             TaskManager.lock.lock()
#             try:
#                 TaskManager.tasks.discard(self)
#             finally:
#                 TaskManager.lock.unlock()
#
#     def cancel(self):
#         self._is_cancelled = True
#         if self.task:
#             self.task.cancel()
#
#
# class LoginDialog(QDialog):
#     def __init__(self):
#         super().__init__()
#         self.shop_id = None
#         self.session = None
#         self.setup_ui()
#
#     def setup_ui(self):
#         self.setWindowTitle('Авторизация')
#         self.setWindowIcon(QIcon("logo.png"))
#         self.setFixedSize(300, 150)
#         layout = QFormLayout(self)
#
#         self.password_input = QLineEdit()
#         self.password_input.setEchoMode(QLineEdit.Password)
#
#         buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
#         buttons.accepted.connect(self.start_auth)
#         buttons.rejected.connect(self.reject)
#
#         layout.addRow("Пароль:", self.password_input)
#         layout.addRow(buttons)
#
#     def start_auth(self):
#         self.session = aiohttp.ClientSession()
#         worker = AsyncWorker(self.authenticate())
#         worker.finished.connect(self.accept)
#         worker.error.connect(self.handle_error)
#         worker.task = asyncio.create_task(worker.run())
#
#     async def authenticate(self):
#         password = self.password_input.text()
#         if not password:
#             raise ValueError("Введите пароль")
#
#         hashed = hashlib.sha256(password.encode()).hexdigest()
#         async with self.session.get(f"{API_URL}/shop/{hashed}") as resp:
#             if resp.status != 200:
#                 raise PermissionError("Неверный пароль")
#             data = await resp.json()
#             self.shop_id = data["ID_shop"]
#
#     def handle_error(self, message):
#         QMessageBox.critical(self, "Ошибка", message)
#         logging.error(f"AUTH ERROR: {message}")
#         asyncio.create_task(self.session.close())
#
#     def closeEvent(self, event):
#         if self.session:
#             asyncio.create_task(self.session.close())
#         super().closeEvent(event)
#
#
# class FileReceiverApp(QWidget):
#     def __init__(self, session, shop_id):
#         super().__init__()
#         self.session = session
#         self.shop_id = shop_id
#         self.file_cache = set()
#         self.current_items = {}
#         self.init_ui()
#         self.setup_timers()
#         sys.excepthook = self.handle_exception
#
#     def handle_exception(self, exc_type, exc_value, exc_traceback):
#         logging.error("Неперехваченное исключение", exc_info=(exc_type, exc_value, exc_traceback))
#         QMessageBox.critical(self, "Ошибка", str(exc_value))
#
#     def setup_timers(self):
#         self.timer = QTimer()
#         self.timer.timeout.connect(lambda: self.execute_async(self.load_orders()))
#         self.timer.start(30000)
#         self.execute_async(self.load_orders())
#
#     def init_ui(self):
#         self.setWindowIcon(QIcon("logo.png"))
#         self.setWindowTitle('Send to print and pick up!')
#         self.setFixedSize(1000, 800)
#
#         main_layout = QVBoxLayout()
#         control_layout = QHBoxLayout()
#
#         self.refresh_btn = QPushButton("Обновить список")
#         self.refresh_btn.clicked.connect(lambda: self.execute_async(self.load_orders()))
#
#         control_layout.addWidget(self.refresh_btn)
#         control_layout.addItem(QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
#
#         self.received_list = QListWidget()
#         self.ready_list = QListWidget()
#
#         main_layout.addLayout(control_layout)
#         main_layout.addWidget(QLabel('Полученные файлы:'))
#         main_layout.addWidget(self.received_list)
#         main_layout.addWidget(QLabel('Готовые к выдаче:'))
#         main_layout.addWidget(self.ready_list)
#
#         self.setLayout(main_layout)
#
#     def execute_async(self, coro):
#         worker = AsyncWorker(coro)
#         worker.finished.connect(self.handle_response)
#         worker.error.connect(self.show_error)
#         worker.task = asyncio.create_task(worker.run())
#
#     async def load_orders(self):
#         try:
#             async with self.session.get(
#                     f"{API_URL}/orders",
#                     params={'status': ['получен', 'готов'], 'shop_id': self.shop_id},
#                     timeout=15
#             ) as resp:
#                 if resp.status == 200:
#                     return await resp.json()
#                 return []
#         except Exception as e:
#             logging.error(f"Ошибка запроса: {traceback.format_exc()}")
#             return []
#
#     def handle_response(self, orders):
#         if not orders:
#             return
#
#         valid_orders = []
#         for order in orders:
#             if 'ID' not in order or 'status' not in order:
#                 logging.error(f"Некорректный заказ: {order}")
#                 continue
#             valid_orders.append(order)
#
#         new_orders = {o['ID']: o for o in valid_orders}
#         current_ids = set(self.current_items.keys())
#
#         for order_id in current_ids - new_orders.keys():
#             self.remove_order_widget(order_id)
#
#         for order_id, order in new_orders.items():
#             if order_id not in current_ids:
#                 self.add_order_widget(order)
#             else:
#                 self._update_existing_widget(order)
#
#     def add_order_widget(self, order):
#         QTimer.singleShot(0, lambda: self._safe_add_widget(order))
#
#     def _update_existing_widget(self, order):
#         item, widget = self.current_items.get(order['ID'], (None, None))
#         if not widget:
#             return
#
#         try:
#             label = widget.findChild(QLabel)
#             if label:
#                 label.setText(f"Заказ №{order['ID']}: {order['file_path']}")
#
#             layout = widget.layout()
#             for i in reversed(range(layout.count())):
#                 layout.itemAt(i).widget().deleteLater()
#
#             status = order['status'].lower().strip()
#             buttons = []
#             if status == 'получен':
#                 buttons = [
#                     ("Печать", lambda _, o=order: self.execute_async(self.print_file(o))),
#                     ("Готово", lambda _, o=order: self.execute_async(self.update_status(o['ID'], 'готов')))
#                 ]
#             elif status == 'готов':
#                 buttons = [
#                     ("Выдать", lambda _, o=order: self.execute_async(self.update_status(o['ID'], 'выдан')))
#                 ]
#
#             for btn_text, callback in buttons:
#                 btn = QPushButton(btn_text)
#                 btn.clicked.connect(callback)
#                 layout.addWidget(btn)
#
#         except Exception as e:
#             logging.error(f"Ошибка обновления виджета: {traceback.format_exc()}")
#
#     def _safe_add_widget(self, order):
#         try:
#             # 1. Проверка обязательных полей
#             required_fields = ['ID', 'status', 'file_path']
#             for field in required_fields:
#                 if field not in order:
#                     logging.error(f"Отсутствует поле '{field}': {order}")
#                     return
#
#             # 2. Нормализация статуса
#             status = order['status'].strip().lower()
#             if status not in {'получен', 'готов'}:
#                 logging.warning(f"Неизвестный статус: {status}")
#                 return
#
#             # 3. Создание элементов интерфейса
#             item = QListWidgetItem()
#             color = "#FF0000" if status == 'получен' else "#00FF00"
#
#             # 4. Динамическое создание кнопок
#             buttons = []
#             if status == 'получен':
#                 buttons = [
#                     ("Печать", lambda: self.execute_async(self.print_file(order))),
#                     ("Готово", lambda: self.execute_async(
#                         self.update_status(order['ID'], 'готов')))
#                 ]
#             else:
#                 buttons = [
#                     ("Выдать", lambda: self.execute_async(
#                         self.update_status(order['ID'], 'выдан')))
#                 ]
#
#             # 5. Создание виджета с проверкой
#             widget = self.create_order_widget(order, color, buttons)
#             if not widget:
#                 return
#
#             # 6. Добавление в соответствующий список
#             target_list = self.received_list if status == 'получен' else self.ready_list
#             target_list.addItem(item)
#             target_list.setItemWidget(item, widget)
#
#             # 7. Обновление кэша
#             self.current_items[order['ID']] = (item, widget)
#
#             # 8. Загрузка файла при необходимости
#             if status == 'получен' and order['file_path'] not in self.file_cache:
#                 self.execute_async(self.download_file(order['file_path']))
#
#         except Exception as e:
#             logging.error(f"Ошибка создания виджета: {traceback.format_exc()}")
#
#     def create_order_widget(self, order, color, buttons):
#         try:
#             widget = QWidget()
#             layout = QHBoxLayout()
#
#             # 9. Форматирование текста
#             file_name = os.path.basename(order['file_path'])
#             label_text = f"Заказ №{order['ID']}: {file_name}"
#             label = QLabel(label_text)
#             label.setStyleSheet(f"color: {color}; font-weight: bold;")
#             layout.addWidget(label)
#
#             # 10. Создание кнопок с фиксацией контекста
#             for btn_text, callback in buttons:
#                 btn = QPushButton(btn_text)
#                 btn.clicked.connect(callback)  # Используем привязку через functools.partial
#                 layout.addWidget(btn)
#
#             widget.setLayout(layout)
#             return widget
#
#         except Exception as e:
#             logging.error(f"Ошибка создания виджета: {traceback.format_exc()}")
#             return None
#
#     def remove_order_widget(self, order_id):
#         QTimer.singleShot(0, lambda: self._safe_remove_widget(order_id))
#
#     def _safe_remove_widget(self, order_id):
#         if order_id in self.current_items:
#             item, widget = self.current_items.pop(order_id)
#             list_widget = item.listWidget()
#             if list_widget:
#                 list_widget.takeItem(list_widget.row(item))
#
#     def closeEvent(self, event):
#         TaskManager.lock.lock()
#         try:
#             for task in TaskManager.tasks:
#                 task.cancel()
#         finally:
#             TaskManager.lock.unlock()
#
#         if self.session:
#             asyncio.create_task(self.session.close())
#         super().closeEvent(event)
#
#     async def download_file(self, filename):
#         try:
#             filepath = os.path.join(DOWNLOAD_DIR, filename)
#             if filename in self.file_cache:
#                 return
#
#             async with self.session.get(f"{API_URL}/files/{filename}", timeout=10) as resp:
#                 if resp.status == 200:
#                     async with aiofiles.open(filepath, 'wb') as f:
#                         async for chunk in resp.content.iter_chunked(8192):
#                             await f.write(chunk)
#                     self.file_cache.add(filename)
#         except Exception as e:
#             self.show_error(f"Ошибка загрузки: {traceback.format_exc()}")
#
#     async def print_file(self, order):
#         try:
#             filepath = os.path.join(DOWNLOAD_DIR, order['file_path'])
#             if sys.platform == "win32":
#                 os.startfile(filepath)
#             else:
#                 import subprocess
#                 subprocess.Popen([filepath], shell=True)
#         except Exception as e:
#             self.show_error(f"Ошибка печати: {traceback.format_exc()}")
#
#     async def update_status(self, order_id, new_status):
#         try:
#             async with self.session.post(
#                     f"{API_URL}/orders/{order_id}/complete",
#                     json={'status': new_status},
#                     timeout=10
#             ) as resp:
#                 if resp.status != 200:
#                     raise ConnectionError(await resp.text())
#                 await self.load_orders()
#         except Exception as e:
#             self.show_error(f"Ошибка обновления: {traceback.format_exc()}")
#
#     def show_error(self, message):
#         QMessageBox.critical(self, "Ошибка", message)
#
#
# async def main():
#     login = LoginDialog()
#     login.show()
#
#     future = asyncio.Future()
#     login.finished.connect(future.set_result)
#     await future
#
#     if login.result() == QDialog.Accepted and login.shop_id:
#         window = FileReceiverApp(login.session, login.shop_id)
#         window.show()
#         return window
#     return None
#
#
# if __name__ == '__main__':
#     app = QApplication(sys.argv)
#     loop = qasync.QEventLoop(app)
#     asyncio.set_event_loop(loop)
#
#     try:
#         with loop:
#             main_window = loop.run_until_complete(main())
#             if main_window:
#                 loop.run_forever()
#             else:
#                 loop.stop()
#     except Exception as e:
#         logging.critical(f"Fatal error: {traceback.format_exc()}")
#     finally:
#         loop.close()
#     sys.exit(0)