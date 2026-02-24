import sys
import requests
import hashlib
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QLabel, QLineEdit, QPushButton, QMessageBox)
from PyQt5.QtCore import Qt
import logging
from dotenv import load_dotenv
import os

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

env_path = os.path.join(os.path.dirname(__file__), 'config.env')
load_dotenv(dotenv_path=env_path)

API_URL = os.getenv("API_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")


class ShopApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Добавление магазина")
        self.setGeometry(100, 100, 400, 300)
        self.initUI()

    def initUI(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout()

        # Поля ввода
        self.name_input = QLineEdit()
        self.address_input = QLineEdit()
        self.w_hours_input = QLineEdit()
        self.bw_price_input = QLineEdit()
        self.color_price_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)

        fields = [
            ("Название магазина:", self.name_input),
            ("Адрес:", self.address_input),
            ("Часы работы:", self.w_hours_input),
            ("Цена черно-белая:", self.bw_price_input),
            ("Цена цветная:", self.color_price_input),
            ("Пароль:", self.password_input),
        ]

        for label_text, field in fields:
            layout.addWidget(QLabel(label_text))
            layout.addWidget(field)

        # Кнопка отправки
        self.submit_button = QPushButton("Добавить магазин")
        self.submit_button.clicked.connect(self.add_shop)
        layout.addWidget(self.submit_button)

        central_widget.setLayout(layout)

    def add_shop(self):
        # Собираем данные
        name = self.name_input.text().strip()
        address = self.address_input.text().strip()
        w_hours = self.w_hours_input.text().strip()
        bw_price = self.bw_price_input.text().strip()
        color_price = self.color_price_input.text().strip()
        password = self.password_input.text().strip()

        # Проверка заполнения
        if not all([name, address, w_hours, bw_price, color_price, password]):
            QMessageBox.warning(self, "Ошибка", "Все поля должны быть заполнены!")
            return

        # Проверка числовых значений
        try:
            bw_price = float(bw_price)
            color_price = float(color_price)
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Цены должны быть числовыми!")
            return

        # Хеширование пароля (как в desktop_app)
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        # Формируем JSON для отправки на сервер
        payload = {
            "name": name,
            "address": address,
            "w_hours": w_hours,
            "price_bw": bw_price,
            "price_cl": color_price,
            "password": password_hash,
        }

        # Отправляем POST-запрос
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Admin-Key": ADMIN_API_KEY
            }

            response = requests.post(
                f"{API_URL}/shops",
                json=payload,
                timeout=10,
                headers=headers
            )

            if response.status_code == 201:
                # Очищаем поля при успехе
                self.name_input.clear()
                self.address_input.clear()
                self.w_hours_input.clear()
                self.bw_price_input.clear()
                self.color_price_input.clear()
                self.password_input.clear()

                QMessageBox.information(self, "Успех", "Магазин успешно добавлен!")
                logger.info("Shop added via API")
            elif response.status_code == 409:
                QMessageBox.warning(self, "Ошибка", "Магазин с таким паролем уже существует")
            else:
                # Пытаемся извлечь детали ошибки из ответа сервера
                try:
                    error_msg = response.json().get("detail", "Неизвестная ошибка")
                except:
                    error_msg = response.text
                QMessageBox.critical(self, "Ошибка сервера", f"Код {response.status_code}: {error_msg}")
                logger.error(f"Server error: {response.status_code} - {error_msg}")

        except requests.exceptions.ConnectionError:
            QMessageBox.critical(self, "Ошибка", "Нет подключения к серверу")
        except requests.exceptions.Timeout:
            QMessageBox.critical(self, "Ошибка", "Сервер не отвечает")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Неизвестная ошибка: {str(e)}")
            logger.exception("Unexpected error")

    def closeEvent(self, event):
        # Ничего не закрываем, соединений с БД больше нет
        event.accept()


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        window = ShopApp()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        QMessageBox.critical(None, "Ошибка", f"Критическая ошибка: {e}")