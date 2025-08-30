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

API_URL = "https://pugnaciously-quickened-gobbler.cloudpub.ru"
DOWNLOAD_DIR = os.path.abspath('downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename='desktop_app.log'
)


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS  # Временная папка PyInstaller
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class FileReceiverApp(QWidget):
    def __init__(self, shop_info: dict):
        super().__init__()
        self.is_refreshing = False
        self.shop_info = shop_info
        self.file_cache = set()
        self.current_items = {}
        self.init_ui()
        self.setup_timers()

        self.current_downloads = {}
        # Убрали downloaded_files, так как будем проверять наличие файла на диске
        self.load_existing_files()

    def init_ui(self):

        self.setStyleSheet("""
            QWidget {
                font-size: 14px;  /* Размер шрифта по умолчанию */
            }
            QLabel {
                font-size: 15px;  /* Увеличенный шрифт для меток */
            }
            QPushButton {
                font-size: 13px;  /* Размер шрифта для кнопок */
            }
            QListWidget {
                font-size: 14px;  /* Размер шрифта в списках */
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
        self.menu_btn.setText("☰")  # Иконка меню
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        # Создаем меню
        menu = QMenu()

        # Добавляем пункты меню
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

    def show_instructions(self):
        QMessageBox.information(self, "Инструкция",
                                "1. Для обновления списка заказов нажмите кнопку 'Обновить список'\n(По умолчанию список обновляется каждые 3 минуты)\n"
                                "2. Перед печатью посмотрите информацию о заказе(тип печати, комментарий). Для просмотра нажмите кнопку 'Информация'\n"
                                "3. Для получения достпупа к файлу нажмите кнопку 'Загрузить'\n"
                                "4. После успешной печати измените статус на 'Готово'\n"
                                "5. Перед тем, как отдавать распечатку сверьте код выдачи по кнопке 'Код выдачи'\n"
                                "6. После успешной проверки отдайте распечатку клиенту и нажмите 'Выдать'")

    def show_contacts(self):
        QMessageBox.information(self, "Контакты",
                                "Техническая поддержка:\n"
                                "1. Герман Андреевич\n"
                                "   Телефон: +7 (920) 021-91-71\n"
                                "   Email: german7352@gmail.com\n"
                                "   Telegram: @shmoshlover\n"
                                "2. Михаил Валерьевич\n"
                                "   Телефон: +7 (953) 575-43-11\n"
                                "   Email: Somik.228@yandex.ru\n"
                                "   Telegram: @Somik288\n"
                                "По любым вопросам рекомендуем обращаться в Telegram")

    @asyncSlot()
    async def on_refresh_clicked(self):
        if self.is_refreshing:
            return  # Если обновление уже идет, игнорируем нажатие

        try:
            self.is_refreshing = True
            self.refresh_btn.setEnabled(False)  # Блокируем кнопку
            self.refresh_btn.setText("Обновление...")

            await self.load_orders()  # Основная логика

        except Exception as e:
            self.show_error(f"Ошибка: {str(e)}")
        finally:
            self.is_refreshing = False
            self.refresh_btn.setEnabled(True)  # Разблокируем кнопку
            self.refresh_btn.setText("Обновить список")

    def setup_timers(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_timer_timeout)
        self.timer.start(180000)
        self.on_timer_timeout()

    @asyncSlot()
    async def on_timer_timeout(self):
        await self.load_orders()

    @asyncSlot()
    async def handle_download_or_open(self, order):
        """Обработчик загрузки или открытия файла"""
        order_id = order['ID']

        # Определяем путь к файлу
        file_extension = os.path.splitext(order['file_path'])[1]
        filename = f"order_{order_id}{file_extension}"
        filepath = os.path.join(DOWNLOAD_DIR, filename)

        # Если файл уже существует, просто открываем папку
        if os.path.exists(filepath):
            self.open_downloads_folder()
            return

        # Если файла нет, скачиваем его
        # Блокируем кнопку для этого заказа
        self.current_downloads[order_id] = True
        await self.load_orders()  # Обновляем список

        try:
            async with aiohttp.ClientSession() as session:
                # Запрашиваем загрузку файла
                async with session.post(
                        f"{API_URL}/orders/{order_id}/download"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        file_url = data['file_url']

                        # Скачиваем файл
                        downloaded_filepath = await self.download_file(file_url, filename)

                        if downloaded_filepath:
                            # Открываем папку downloads после загрузки
                            self.open_downloads_folder()
                        else:
                            self.show_error("Не удалось скачать файл")
                    else:
                        error_text = await resp.text()
                        self.show_error(f"Ошибка загрузки (код {resp.status}): {error_text}")
                        logging.error(f"Ошибка загрузки файла заказа {order_id}: {error_text}")
        except Exception as e:
            self.show_error(f"Ошибка загрузки: {str(e)}")
        finally:
            # Разблокируем кнопку
            if order_id in self.current_downloads:
                del self.current_downloads[order_id]
            await self.load_orders()  # Обновляем список

    async def download_file(self, url: str, filename: str) -> Optional[str]:
        try:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            # Создаем коннектор с отключенной проверкой SSL
            # connector = aiohttp.TCPConnector(ssl=False)  connector=connector
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        async with aiofiles.open(filepath, 'wb') as f:
                            await f.write(content)
                        return filepath
        except Exception as e:
            logging.error(f"Download error: {str(e)}")
            traceback.print_exc()
        return None

    def validate_order(self, order):
        required_fields = ['ID', 'status', 'file_path']
        return all(field in order for field in required_fields)

    def load_existing_files(self):
        # Больше не нужно отслеживать скачанные файлы
        pass

    async def load_orders(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{API_URL}/orders",
                        params={'status': ['received', 'ready'], 'shop_id': self.shop_info['ID_shop']},
                        timeout=10, headers={"Cache-Control": "no-cache"}
                ) as resp:
                    if resp.status == 200:
                        orders = await resp.json()
                        print(orders)
                        unique_orders = {order['ID']: order for order in orders}.values()
                        self.handle_orders(list(unique_orders))
        except Exception as e:
            self.show_error(f"Ошибка запроса: {str(e)}")

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

                item.setSizeHint(widget.sizeHint())
                target_list = self.received_list if order['status'] == 'received' else self.ready_list
                target_list.addItem(item)
                target_list.setItemWidget(item, widget)

                self.current_items[order['ID']] = (item, widget)

            except Exception as e:
                logging.error(f"Ошибка обработки заказа {order.get('ID', 'неизвестно')}: {traceback.format_exc()}")

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
            # Всегда показываем кнопку "Загрузить"
            btn_download = QPushButton("Файл")
            if order['ID'] in self.current_downloads:
                btn_download.setEnabled(False)
                btn_download.setText("Загрузка...")
            else:
                btn_download.clicked.connect(lambda: self.handle_download_or_open(order))

            btn_ready = QPushButton("Готово")
            btn_ready.clicked.connect(lambda: self.confirm_status_change(
                order['ID'],
                'ready',
                f"Подтвердите изменение статуса заказа №{order['ID']} на 'Готово'"
            ))
            btn_info = QPushButton("Информация")
            btn_info.clicked.connect(lambda: self.show_order_info(order))
            buttons = [btn_info, btn_download, btn_ready]
        else:
            btn_complete = QPushButton("Выдать")
            btn_complete.clicked.connect(lambda: self.confirm_status_change(
                order['ID'],
                'completed',
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
                min-width: 120px;  /* Фиксированная ширина с запасом */
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
            self,
            'Подтверждение',
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.update_status(order_id, new_status)

    def show_order_info(self, order):
        info_message = f"Заказ №{order['ID']}\n" \
                       f"Тип печати: {order['color']}\n" \
                       f"Комментарий: {order.get('note', 'Нет информации')}\n" \
                       f"Стоимость печати: {order['price']} руб."
        QMessageBox.information(self, "Информация о заказе", info_message)

    def show_con_code(self, order):
        info_message = f"Заказ №{order['ID']}\n" \
                       f"Код подтверждения: {order['con_code']}\n" \
                       f"Стоимость печати: {order['price']} руб."
        QMessageBox.information(self, "Проверочный код", info_message)

    @asyncSlot()
    async def update_status(self, order_id, new_status):
        try:
            async with aiohttp.ClientSession() as session:
                # Обновление статуса на сервере
                endpoint = "ready" if new_status == "ready" else "complete"
                async with session.post(f"{API_URL}/orders/{order_id}/{endpoint}"):
                    pass

                # Обновление списка
                await self.load_orders()

        except Exception as e:
            self.show_error(f"Ошибка: {str(e)}")
            logging.error(traceback.format_exc())

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        # Удаление всех файлов из папки downloads
        if os.path.exists(DOWNLOAD_DIR):
            for filename in os.listdir(DOWNLOAD_DIR):
                file_path = os.path.join(DOWNLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        logging.info(f"Файл {filename} удален.")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла {filename}: {str(e)}")

        super().closeEvent(event)


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.shop_info = {}
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
                self.shop_info = response.json()
                self.accept()
            else:
                QMessageBox.critical(self, "Ошибка", "Неверный пароль")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))


if __name__ == '__main__':
    app = QApplication(sys.argv)

    # Устанавливаем глобальный стиль для всех QMessageBox
    app.setStyleSheet("""
        QMessageBox {
            font-size: 14px;
        }""")

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        login_dialog = LoginDialog()
        if login_dialog.exec() == QDialog.DialogCode.Accepted and login_dialog.shop_info:
            window = FileReceiverApp(login_dialog.shop_info)
            window.show()
            loop.run_forever()