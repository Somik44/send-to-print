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

API_URL = "https://helpfully-accustomed-falcon.cloudpub.ru"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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


class AuthManager:
    def __init__(self):
        self.access_token = None
        self.token_expires = None
        self.shop_info = None

    def is_token_valid(self):
        # Просто проверяем, что токен есть, а срок проверяет сервер
        return self.access_token is not None

    async def make_authenticated_request(self, method: str, url: str, **kwargs):
        """Выполняет авторизованный запрос с JWT токеном"""
        logging.info(f"Making authenticated request, token valid: {self.is_token_valid()}")

        # ВКЛЮЧИТЬ проверку валидности токена
        if not self.is_token_valid():
            raise Exception("Token expired or invalid")

        headers = kwargs.get('headers', {})
        headers['Authorization'] = f'Bearer {self.access_token}'
        kwargs['headers'] = headers

        async with aiohttp.ClientSession() as session:
            response = await session.request(method, url, **kwargs)
            logging.info(f"Request to {url} returned status: {response.status}")
            return response

    async def login(self, password: str) -> bool:
        try:
            hashed = hashlib.sha256(password.encode()).hexdigest()

            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{API_URL}/auth/login",
                        data={"password_hash": hashed},
                        timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.access_token = data['access_token']
                        self.shop_info = data['shop_info']
                        return True
                    else:
                        return False
        except Exception:
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
        # self.password_input.returnPressed.connect(self.authenticate)

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

        # Блокируем UI во время аутентификации
        self.setEnabled(False)
        buttons = self.findChildren(QDialogButtonBox)[0]
        buttons.setEnabled(False)

        # Используем ТОЛЬКО синхронный запрос
        self.sync_authenticate(password)

    def sync_authenticate(self, password: str):
        """Синхронная аутентификация с упрощенной обработкой ошибок"""
        try:
            hashed = hashlib.sha256(password.encode()).hexdigest()

            # Простой запрос без диагностики
            new_response = requests.post(
                f"{API_URL}/auth/login",
                data={"password_hash": hashed},
                timeout=10
            )

            if new_response.status_code == 200:
                data = new_response.json()
                self.auth_manager.access_token = data['access_token']
                self.auth_manager.shop_info = data['shop_info']
                self.accept()
            elif new_response.status_code == 401:
                QMessageBox.critical(self, "Ошибка", "Неверный пароль")
            else:
                QMessageBox.critical(self, "Ошибка", "Ошибка подключения")

        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "Ошибка", "Нет подключения к интернету")
        except requests.exceptions.Timeout:
            QMessageBox.critical(self, "Ошибка", "Сервер не отвечает")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", "Ошибка подключения")
        finally:
            # Восстанавливаем UI
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
        self.file_cache = set()
        self.current_items = {}

        # Проверяем что shop_info не None
        if not self.shop_info:
            logging.error("shop_info is None in FileReceiverApp constructor!")
            raise ValueError("shop_info cannot be None")

        logging.info(f"Initializing FileReceiverApp for shop: {self.shop_info}")

        self.init_ui()
        self.setup_timers()

        self.current_downloads = {}
        self.load_existing_files()

    def init_ui(self):
        self.setStyleSheet("""
            QWidget {
                font-size: 14px;
            }
            QLabel {
                font-size: 15px;
            }
            QPushButton {
                font-size: 13px;
            }
            QListWidget {
                font-size: 14px;
            }
        """)

        self.setWindowIcon(QIcon(resource_path('logo.png')))
        self.setWindowTitle('Send to print and pick up!')
        self.setMinimumSize(800, 600)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Создаем верхнюю панель с кнопками
        top_panel = QHBoxLayout()

        # Кнопка обновления
        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        top_panel.addWidget(self.refresh_btn)

        shop_text = f"Точка {self.shop_info['name']} по адресу {self.shop_info['address']}"
        self.shop_label = QLabel(shop_text)
        self.shop_label.setStyleSheet("""
            QLabel {
                font-size: 16px;
                color: #555;
                padding: 5px 15px;
                border-left: 2px solid #ddd;
            }
        """)
        top_panel.addWidget(self.shop_label)

        # Растягивающийся элемент
        top_panel.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        # Кнопка меню
        self.menu_btn = QToolButton()
        self.menu_btn.setText("☰")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        # Создаем меню
        menu = QMenu()
        instruction_action = menu.addAction("Инструкция")
        instruction_action.triggered.connect(self.show_instructions)
        contacts_action = menu.addAction("Контакты")
        contacts_action.triggered.connect(self.show_contacts)
        self.menu_btn.setMenu(menu)
        top_panel.addWidget(self.menu_btn)

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

        main_layout.addLayout(top_panel)
        main_layout.addWidget(QLabel('Полученные файлы:'))
        main_layout.addWidget(self.received_list)
        main_layout.addWidget(QLabel('Готовые к выдаче:'))
        main_layout.addWidget(self.ready_list)

        self.setLayout(main_layout)
        logging.info("FileReceiverApp UI initialized successfully")

    def show_instructions(self):
        QMessageBox.information(self, "Инструкция",
                                "1. Для обновления списка заказов нажмите кнопку 'Обновить список'\n"
                                "2. Перед печатью посмотрите информацию о заказе\n"
                                "3. Для получения доступа к файлу нажмите кнопку 'Файл'\n"
                                "4. После печати измените статус на 'Готово'\n"
                                "5. Перед выдачей сверьте код выдачи\n"
                                "6. После проверки нажмите 'Выдать'")

    def show_contacts(self):
        QMessageBox.information(self, "Контакты",
                                "Техническая поддержка:\n"
                                "Телефон: +7 (920) 021-91-71\n"
                                "Telegram: @shmoshlover")

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
        self.timer.start(350000)  # 5 минут
        # Запускаем первоначальную загрузку через QTimer
        QTimer.singleShot(0, lambda: asyncio.ensure_future(self.load_orders()))

    @asyncSlot()
    async def on_timer_timeout(self):
        await self.load_orders()

    @asyncSlot()
    async def handle_download_or_open(self, order):
        """Обработчик загрузки или открытия файла (рабочая версия из старого кода)"""
        order_id = order['ID']
        filename = order['file_path']
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        # Если файл уже существует, просто открываем папку
        if os.path.exists(filepath):
            self.open_downloads_folder()
            return

        # Прямая загрузка файла с сервера через защищенный эндпоинт
        file_url = f"{API_URL}/files/{filename}"

        # Блокируем кнопку для этого заказа
        self.current_downloads[order_id] = True
        await self.load_orders()  # Обновляем список

        try:
            # Скачиваем файл напрямую
            success = await self.download_file(file_url, filename)

            if success:
                # Открываем папку downloads после загрузки
                self.open_downloads_folder()
            else:
                self.show_error("Не удалось скачать файл")
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")
        finally:
            # Разблокируем кнопку
            if order_id in self.current_downloads:
                del self.current_downloads[order_id]
            await self.load_orders()  # Обновляем список

    async def download_file(self, url: str, filename: str) -> bool:
        """Скачивание файла с поддержкой JWT токена"""
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)

            # Добавляем заголовок авторизации для защищенного эндпоинта
            headers = {}
            if self.auth_manager.access_token:
                headers['Authorization'] = f'Bearer {self.auth_manager.access_token}'

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(content)
                        return True
                    else:
                        logging.error(f"Download failed with status: {resp.status}")
                        return False
        except Exception as e:
            logging.error(f"Download error: {str(e)}")
            traceback.print_exc()
            return False

    def validate_order(self, order):
        required_fields = ['ID', 'status', 'file_path']
        return all(field in order for field in required_fields)

    def load_existing_files(self):
        pass

    @asyncSlot()
    async def load_orders(self):
        try:
            logging.info("Loading orders...")
            logging.info(f"Shop info: {self.shop_info}")
            logging.info(f"Token: {self.auth_manager.access_token}")

            # Сначала проверим токен через специальный эндпоинт
            try:
                verify_resp = await self.auth_manager.make_authenticated_request(
                    'GET', f"{API_URL}/auth/verify"
                )
                if verify_resp.status == 200:
                    verify_data = await verify_resp.json()
                    logging.info(f"Token verification: {verify_data}")
                else:
                    logging.warning(f"Token verification failed: {verify_resp.status}")
            except Exception as e:
                logging.error(f"Token verification error: {str(e)}")

            # Затем загружаем заказы
            resp = await self.auth_manager.make_authenticated_request(
                'GET',
                f"{API_URL}/orders",
                params={'status': ['received', 'ready']}
            )

            if resp.status == 200:
                orders = await resp.json()
                logging.info(f"Loaded {len(orders)} orders")
                unique_orders = {order['ID']: order for order in orders}.values()
                self.handle_orders(list(unique_orders))
            elif resp.status == 401:
                logging.warning("Session expired - received 401 from server")
                self.show_error("Сессия истекла. Пожалуйста, перезайдите.")
                self.close()
            else:
                error_text = await resp.text()
                logging.error(f"Failed to load orders: {resp.status}, {error_text}")
                self.show_error(f"Ошибка загрузки заказов: {resp.status}")
        except Exception as e:
            logging.error(f"Load orders error: {str(e)}\n{traceback.format_exc()}")
            self.show_error(f"Ошибка запроса: {str(e)}")

    def handle_orders(self, orders):
        try:
            self.received_list.clear()
            self.ready_list.clear()
            self.current_items.clear()

            for order in orders:
                if not self.validate_order(order):
                    continue

                item = QListWidgetItem()
                widget = self.create_order_widget(order)
                item.setSizeHint(widget.sizeHint())

                target_list = self.received_list if order['status'] == 'received' else self.ready_list
                target_list.addItem(item)
                target_list.setItemWidget(item, widget)
                self.current_items[order['ID']] = (item, widget)

            logging.info(f"Displayed {len(orders)} orders")
        except Exception as e:
            logging.error(f"Handle orders error: {str(e)}\n{traceback.format_exc()}")

    def create_order_widget(self, order):
        widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(10)

        file_name = os.path.basename(order['file_path'])
        label_text = f"Заказ №{order['ID']}: {file_name}"
        label = QLabel(label_text)
        label.setWordWrap(True)
        color = "#dc3545" if order['status'] == 'received' else "#28a745"
        label.setStyleSheet(f"QLabel {{ color: {color}; font-weight: 600; font-size: 13px; padding: 5px; }}")

        buttons = []
        if order['status'] == 'received':
            btn_download = QPushButton("Файл")
            if order['ID'] in self.current_downloads:
                btn_download.setEnabled(False)
                btn_download.setText("Загрузка...")
            else:
                btn_download.clicked.connect(lambda: self.handle_download_or_open(order))

            btn_ready = QPushButton("Готово")
            btn_ready.clicked.connect(lambda: self.confirm_status_change(
                order['ID'], 'ready',
                f"Подтвердите изменение статуса заказа №{order['ID']} на 'Готово'"
            ))
            btn_info = QPushButton("Информация")
            btn_info.clicked.connect(lambda: self.show_order_info(order))
            buttons = [btn_info, btn_download, btn_ready]
        else:
            btn_complete = QPushButton("Выдать")
            btn_complete.clicked.connect(lambda: self.confirm_status_change(
                order['ID'], 'completed',
                f"Подтвердите выдачу заказа №{order['ID']} клиенту"
            ))
            btn_info = QPushButton("Код выдачи")
            btn_info.clicked.connect(lambda: self.show_con_code(order))
            buttons = [btn_info, btn_complete]

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
        for btn in buttons:
            btn.setStyleSheet(button_style)
            layout.addWidget(btn)

        layout.addWidget(label)
        widget.setLayout(layout)
        return widget

    def open_downloads_folder(self):
        if sys.platform == "win32":
            os.startfile(DOWNLOAD_DIR)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", DOWNLOAD_DIR])

    def confirm_status_change(self, order_id, new_status, message):
        reply = QMessageBox.question(
            self, 'Подтверждение', message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # Используем QTimer для запуска асинхронной задачи
            QTimer.singleShot(0, lambda: asyncio.ensure_future(self.update_status(order_id, new_status)))

    def show_order_info(self, order):
        info_message = f"Заказ №{order['ID']}\nТип печати: {order['color']}\nКомментарий: {order.get('note', 'Нет информации')}\nСтоимость печати: {order['price']} руб."
        QMessageBox.information(self, "Информация о заказе", info_message)

    def show_con_code(self, order):
        info_message = f"Заказ №{order['ID']}\nКод подтверждения: {order['con_code']}\nСтоимость печати: {order['price']} руб."
        QMessageBox.information(self, "Проверочный код", info_message)

    @asyncSlot()
    async def update_status(self, order_id, new_status):
        try:
            endpoint = "ready" if new_status == "ready" else "complete"
            resp = await self.auth_manager.make_authenticated_request(
                'POST', f"{API_URL}/orders/{order_id}/{endpoint}"
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


def main():
    """Главная функция приложения"""
    try:
        logging.info("Starting application...")
        app = QApplication(sys.argv)

        app.setStyleSheet("""
            QMessageBox {
                font-size: 14px;
            }""")

        # Создаем и настраиваем event loop
        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)

        # Показываем диалог логина
        login_dialog = LoginDialog()
        result = login_dialog.exec()

        if result == QDialog.DialogCode.Accepted and login_dialog.auth_manager.shop_info:
            logging.info("Login successful, creating main window...")
            window = FileReceiverApp(login_dialog.auth_manager)
            window.show()
            logging.info("Main window shown, starting event loop...")

            # Запускаем event loop
            with loop:
                loop.run_forever()
        else:
            logging.info("Login failed or cancelled, exiting...")
            sys.exit(0)

    except Exception as e:
        logging.error(f"Fatal error in main: {str(e)}\n{traceback.format_exc()}")
        QMessageBox.critical(None, "Фатальная ошибка", f"Приложение завершилось с ошибкой:\n{str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
