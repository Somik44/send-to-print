import sys
import os
import logging
import hashlib
import aiohttp
import asyncio
import json
import requests
import traceback
import aiofiles
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton,
    QLabel, QMessageBox, QHBoxLayout, QListWidgetItem,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout,
    QSpacerItem, QSizePolicy, QMenu, QToolButton
)
from PyQt6.QtGui import QIcon
import qasync
from qasync import asyncSlot, QEventLoop
from typing import Optional
import jwt
from datetime import datetime, timedelta, timezone
import urllib.request
import ssl
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

API_URL = os.getenv("API_URL")
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

aiohttp_session: Optional[aiohttp.ClientSession] = None

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


async def make_aiohttp_request(method, url, **kwargs):
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        await init_aiohttp_session()
    return await aiohttp_session.request(method, url, **kwargs)


async def init_aiohttp_session():
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        aiohttp_session = aiohttp.ClientSession(
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(
                ssl=False,
                limit=50,
                limit_per_host=10
            )
        )


def make_requests_request(method, url, **kwargs):
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 30
    kwargs['verify'] = True
    with requests.Session() as session:
        session.trust_env = True
        return session.request(method, url, **kwargs)


class AuthManager:
    def __init__(self):
        self.access_token = None
        self.token_expires = None
        self.shop_info = None

    def is_token_valid(self):
        return self.access_token is not None

    async def make_authenticated_request(self, method: str, url: str, **kwargs):
        logging.info(f"Making authenticated request through proxy, token valid: {self.is_token_valid()}")
        if not self.is_token_valid():
            raise Exception("Token expired or invalid")
        headers = kwargs.get('headers', {})
        headers['Authorization'] = f'Bearer {self.access_token}'
        kwargs['headers'] = headers
        response = await make_aiohttp_request(method, url, **kwargs)
        logging.info(f"Request to {url} returned status: {response.status}")
        return response

    async def login(self, password: str) -> bool:
        try:
            hashed = hashlib.sha256(password.encode()).hexdigest()
            response = await make_aiohttp_request(
                'POST',
                f"{API_URL}/auth/login",
                data={"password_hash": hashed},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            if response.status == 200:
                data = await response.json()
                self.access_token = data['access_token']
                self.shop_info = data['shop_info']
                return True
            else:
                logging.error(f"Login failed with status: {response.status}")
                return False
        except Exception as e:
            logging.error(f"Login error: {str(e)}")
            return False


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.auth_manager = AuthManager()
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle('Авторизация')
        self.setWindowIcon(QIcon(resource_path('logo.png')))
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

        self.setEnabled(False)
        buttons = self.findChildren(QDialogButtonBox)[0]
        buttons.setEnabled(False)

        self.sync_authenticate(password)

    def sync_authenticate(self, password: str):
        try:
            hashed = hashlib.sha256(password.encode()).hexdigest()
            response = make_requests_request(
                'POST',
                f"{API_URL}/auth/login",
                data={"password_hash": hashed},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                self.auth_manager.access_token = data['access_token']
                self.auth_manager.shop_info = data['shop_info']
                self.accept()
            elif response.status_code == 401:
                QMessageBox.critical(self, "Ошибка", "Неверный пароль")
            else:
                QMessageBox.critical(self, "Ошибка", f"Ошибка подключения: {response.status_code}")
        except requests.exceptions.ProxyError as e:
            QMessageBox.critical(self, "Ошибка прокси", f"Не удалось подключиться через прокси:\n{str(e)}")
        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "Ошибка", "Нет подключения к интернету")
        except requests.exceptions.Timeout:
            QMessageBox.critical(self, "Ошибка", "Сервер не отвечает")
        except Exception as e:
            logging.error(f"Full traceback during authentication: {traceback.format_exc()}")
            QMessageBox.critical(self, "Критическая ошибка",
                                 f"Произошла непредвиденная ошибка подключения: {str(e)}\n\nОбратитесь в поддержку и проверьте лог-файл desktop_app.log.")
        finally:
            self.setEnabled(True)
            found_buttons = self.findChildren(QDialogButtonBox)
            if found_buttons:
                found_buttons[0].setEnabled(True)


class FileReceiverApp(QWidget):
    def __init__(self, auth_manager: AuthManager):
        super().__init__()
        self.auth_manager = auth_manager
        self.shop_info = auth_manager.shop_info
        self.is_refreshing = False
        self.current_downloads = {}

        if not self.shop_info:
            logging.error("shop_info is None in FileReceiverApp constructor!")
            raise ValueError("shop_info cannot be None")

        logging.info(f"Initializing FileReceiverApp for shop: {self.shop_info}")

        self.init_ui()
        self.setup_timers()

    def init_ui(self):
        self.setStyleSheet("""
            QWidget { font-size: 14px; }
            QLabel { font-size: 15px; }
            QPushButton { font-size: 13px; }
            QListWidget { font-size: 14px; }
        """)

        self.setWindowIcon(QIcon(resource_path('logo.png')))
        self.setWindowTitle('Send to print and pick up!')
        self.setMinimumSize(800, 600)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        top_panel = QHBoxLayout()

        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        top_panel.addWidget(self.refresh_btn)

        shop_text = f"Точка {self.shop_info['name']} по адресу {self.shop_info['address']}"
        self.shop_label = QLabel(shop_text)
        self.shop_label.setStyleSheet("font-size: 16px; color: #555; padding: 5px 15px; border-left: 2px solid #ddd;")
        top_panel.addWidget(self.shop_label)

        top_panel.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        self.menu_btn = QToolButton()
        self.menu_btn.setText("☰")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu()
        instruction_action = menu.addAction("Инструкция")
        instruction_action.triggered.connect(self.show_instructions)
        contacts_action = menu.addAction("Контакты")
        contacts_action.triggered.connect(self.show_contacts)
        proxy_check_action = menu.addAction("Проверить прокси")
        proxy_check_action.triggered.connect(self.check_proxy_settings)

        self.menu_btn.setMenu(menu)
        top_panel.addWidget(self.menu_btn)

        self.orders_list = QListWidget()
        self.orders_list.setStyleSheet("""
            QListWidget { background-color: #ffffff; border: 1px solid #cccccc; border-radius: 4px; }
            QListWidget::item { border-bottom: 1px solid #eeeeee; }
        """)

        main_layout.addLayout(top_panel)
        main_layout.addWidget(QLabel('Текущие заказы:'))
        main_layout.addWidget(self.orders_list)

        self.setLayout(main_layout)
        logging.info("FileReceiverApp UI initialized successfully")

    def show_instructions(self):
        QMessageBox.information(self, "Инструкция",
                                "1. Для обновления списка заказов нажмите кнопку 'Обновить список' "
                                "(По умолчанию обновление происходит раз в 5 минут)\n"
                                "2. Для получения доступа к файлу нажмите кнопку 'Файл'\n"
                                "3. После печати нажмите 'Завершить', чтобы выдать заказ клиенту\n"
                                "4. Подтвердите завершение в диалоговом окне")

    def show_contacts(self):
        QMessageBox.information(self, "Контакты",
                                "Техническая поддержка:\n"
                                "Герман Андреевич\n"
                                "Телефон: +7 (930) 709-80-86\n"
                                "Telegram: @shmoshlover\n\n"
                                "Михаил Валерьевич\n"
                                "Телефон: +7 (953) 575-43-11\n"
                                "Telegram: @Somik288\n\n"
                                "Рекомендуем писать в Telegram, звонить только в экстренных случаях")

    def check_proxy_settings(self):
        try:
            proxies = urllib.request.getproxies()
            if proxies:
                proxy_info = "\n".join([f"{p}: {a}" for p, a in proxies.items()])
                QMessageBox.information(self, "Настройки прокси", f"Обнаружены системные прокси:\n\n{proxy_info}")
            else:
                QMessageBox.information(self, "Настройки прокси", "Системные прокси не обнаружены")
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Ошибка получения настроек прокси:\n{str(e)}")

    @asyncSlot()
    async def on_refresh_clicked(self):
        if self.is_refreshing:
            return
        try:
            self.is_refreshing = True
            self.refresh_btn.setEnabled(False)
            self.refresh_btn.setText("Обновление...")
            await self.load_orders()
        except Exception as e:
            logging.error(f"Refresh error: {str(e)}\n{traceback.format_exc()}")
            self.show_error(f"Ошибка: {str(e)}")
        finally:
            self.is_refreshing = False
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText("Обновить список")

    def setup_timers(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_timer_timeout)
        self.timer.start(300000)
        QTimer.singleShot(0, lambda: asyncio.ensure_future(self.load_orders()))

    @asyncSlot()
    async def on_timer_timeout(self):
        await self.load_orders()

    @asyncSlot()
    async def load_orders(self):
        try:
            logging.info("Loading orders through proxy...")
            resp = await self.auth_manager.make_authenticated_request(
                'GET',
                f"{API_URL}/orders",
                params={'status': ['received']}
            )
            if resp.status == 200:
                orders = await resp.json()
                logging.info(f"Loaded {len(orders)} orders")
                self.handle_orders(orders)
            elif resp.status == 401:
                logging.warning("Session expired")
                self.show_error("Сессия истекла. Перезайдите.")
                self.close()
            else:
                error_text = await resp.text()
                logging.error(f"Failed to load orders: {resp.status}, {error_text}")
                self.show_error(f"Ошибка загрузки заказов: {resp.status}")
            await resp.release()
        except aiohttp.ClientProxyConnectionError as e:
            logging.error(f"Proxy connection error: {str(e)}")
            self.show_error(f"Ошибка подключения через прокси:\n{str(e)}\n\nПроверьте настройки прокси.")
        except Exception as e:
            logging.error(f"Load orders error: {str(e)}\n{traceback.format_exc()}")
            self.show_error(f"Ошибка запроса: {str(e)}")

    def handle_orders(self, orders):
        try:
            self.orders_list.clear()
            for order in orders:
                if not self.validate_order(order):
                    continue
                item = QListWidgetItem()
                widget = self.create_order_widget(order)
                item.setSizeHint(widget.sizeHint())
                self.orders_list.addItem(item)
                self.orders_list.setItemWidget(item, widget)
            logging.info(f"Displayed {len(orders)} orders")
        except Exception as e:
            logging.error(f"Handle orders error: {str(e)}\n{traceback.format_exc()}")

    def validate_order(self, order):
        required_fields = ['ID', 'file_path']
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
        label.setStyleSheet("QLabel { color: #dc3545; font-weight: 600; font-size: 13px; padding: 5px; }")

        btn_file = QPushButton("Файл")
        if order['ID'] in self.current_downloads:
            btn_file.setEnabled(False)
            btn_file.setText("Загрузка...")
        else:
            btn_file.clicked.connect(lambda checked, o=order: self.handle_download_or_open(o))

        btn_complete = QPushButton("Завершить")
        btn_complete.clicked.connect(
            lambda: self.confirm_status_change(
                order['ID'],
                f"Подтвердите завершение заказа №{order['ID']}"
            )
        )

        button_style = """
            QPushButton {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 6px 12px;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #e2e6ea;
            }
        """
        for btn in (btn_file, btn_complete):
            btn.setStyleSheet(button_style)
            layout.addWidget(btn)

        layout.addWidget(label)
        widget.setLayout(layout)
        return widget

    @asyncSlot()
    async def handle_download_or_open(self, order):
        order_id = order['ID']
        filename = order['file_path']
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        if os.path.exists(filepath):
            self.open_downloads_folder()
            return

        file_url = f"{API_URL}/files/{filename}"
        self.current_downloads[order_id] = True
        await self.load_orders()

        try:
            success = await self.download_file(file_url, filename)
            if success:
                self.open_downloads_folder()
            else:
                self.show_error("Не удалось скачать файл")
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")
        finally:
            if order_id in self.current_downloads:
                del self.current_downloads[order_id]
            await self.load_orders()

    async def download_file(self, url: str, filename: str) -> bool:
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            headers = {}
            if self.auth_manager.access_token:
                headers['Authorization'] = f'Bearer {self.auth_manager.access_token}'

            response = await make_aiohttp_request('GET', url, headers=headers)
            if response.status == 200:
                content = await response.read()
                async with aiofiles.open(filepath, 'wb') as f:
                    await f.write(content)
                return True
            else:
                logging.error(f"Download failed with status: {response.status}")
                return False
        except aiohttp.ClientProxyConnectionError as e:
            logging.error(f"Proxy connection error during download: {str(e)}")
            self.show_error(f"Ошибка подключения через прокси: {str(e)}")
            return False
        except Exception as e:
            logging.error(f"Download error: {str(e)}")
            traceback.print_exc()
            return False

    def open_downloads_folder(self):
        if sys.platform == "win32":
            os.startfile(DOWNLOAD_DIR)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", DOWNLOAD_DIR])

    def confirm_status_change(self, order_id, message):
        reply = QMessageBox.question(
            self, 'Подтверждение', message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            QTimer.singleShot(0, lambda: asyncio.ensure_future(self.update_status(order_id)))

    @asyncSlot()
    async def update_status(self, order_id):
        try:
            resp = await self.auth_manager.make_authenticated_request(
                'POST', f"{API_URL}/orders/{order_id}/complete"
            )
            if resp.status == 200:
                await self.load_orders()
            else:
                error_text = await resp.text()
                logging.error(f"Status update failed: {resp.status}, {error_text}")
                self.show_error(f"Ошибка обновления статуса: {resp.status}")
        except Exception as e:
            logging.error(f"Update status error: {str(e)}\n{traceback.format_exc()}")
            self.show_error(f"Ошибка: {str(e)}")

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        logging.info("Closing application, cleaning up downloads...")
        if os.path.exists(DOWNLOAD_DIR):
            for filename in os.listdir(DOWNLOAD_DIR):
                file_path = os.path.join(DOWNLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logging.info(f"Удален файл: {filename}")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла {filename}: {str(e)}")
        super().closeEvent(event)


async def close_aiohttp():
    global aiohttp_session
    if aiohttp_session and not aiohttp_session.closed:
        await aiohttp_session.close()


def main():
    try:
        logging.info("Starting application...")
        app = QApplication(sys.argv)
        app.setStyleSheet("QMessageBox { font-size: 14px; }")

        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)

        login_dialog = LoginDialog()
        result = login_dialog.exec()

        if result == QDialog.DialogCode.Accepted and login_dialog.auth_manager.shop_info:
            logging.info("Login successful, creating main window...")
            window = FileReceiverApp(login_dialog.auth_manager)
            window.show()
            logging.info("Main window shown, starting event loop...")
            with loop:
                try:
                    loop.run_forever()
                finally:
                    logging.info("Shutting down aiohttp session...")
                    loop.run_until_complete(close_aiohttp())
        else:
            logging.info("Login failed or cancelled, exiting...")
            sys.exit(0)
    except Exception as e:
        logging.error(f"Fatal error in main: {str(e)}\n{traceback.format_exc()}")
        QMessageBox.critical(None, "Фатальная ошибка", f"Приложение завершилось с ошибкой:\n{str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()