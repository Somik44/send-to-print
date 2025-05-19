import sys
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox)
from PyQt5.QtCore import Qt
import pymysql
from hashlib import sha256
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class ShopApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Добавление магазина")
        self.setGeometry(100, 100, 400, 300)

        try:
            # Подключение к базе данных через PyMySQL
            self.db_connection = pymysql.connect(
                host="localhost",
                user="root",
                password="3465",
                database="send_to_print",
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
            logger.info("Успешное подключение к базе данных")
        except pymysql.Error as err:
            logger.error(f"Ошибка подключения к базе данных: {err}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось подключиться к базе данных: {err}")
            sys.exit(1)

        self.initUI()
        self.check_existing_passwords()

    def initUI(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout()

        # Поля ввода
        fields = [
            ("Название магазина:", "name_input"),
            ("Адрес:", "address_input"),
            ("Цена черно-белая:", "bw_price_input"),
            ("Цена цветная:", "color_price_input"),
            ("Пароль:", "password_input")
        ]

        for field in fields:
            label_text = field[0]
            attr_name = field[1]
            label = QLabel(label_text)
            input_field = QLineEdit()

            if len(field) > 2:
                input_field.setEchoMode(field[2])

            setattr(self, attr_name, input_field)
            layout.addWidget(label)
            layout.addWidget(input_field)

        # Кнопка отправки
        self.submit_button = QPushButton("Добавить магазин")
        self.submit_button.clicked.connect(self.add_shop)
        layout.addWidget(self.submit_button)

        central_widget.setLayout(layout)

    def check_existing_passwords(self):
        self.existing_hashes = set()
        try:
            with self.db_connection.cursor() as cursor:
                cursor.execute("SELECT password FROM shop")
                for row in cursor:
                    self.existing_hashes.add(row['password'])
        except pymysql.Error as err:
            logger.error(f"Ошибка при загрузке паролей: {err}")
            QMessageBox.warning(self, "Ошибка", f"Ошибка при загрузке паролей: {err}")

    def add_shop(self):
        # Получаем данные из полей ввода
        fields = {
            'name': self.name_input.text().strip(),
            'address': self.address_input.text().strip(),
            'bw_price': self.bw_price_input.text().strip(),
            'color_price': self.color_price_input.text().strip(),
            'password': self.password_input.text().strip()
        }

        # Проверка заполнения полей
        if not all(fields.values()):
            QMessageBox.warning(self, "Ошибка", "Все поля должны быть заполнены!")
            return

        # Проверка числовых значений
        try:
            fields['bw_price'] = float(fields['bw_price'])
            fields['color_price'] = float(fields['color_price'])
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Цены должны быть числовыми!")
            return

        # Хеширование пароля
        password_hash = sha256(fields['password'].encode()).hexdigest()

        # Проверка на уникальность пароля
        if password_hash in self.existing_hashes:
            QMessageBox.warning(self, "Ошибка", "Пароль уже существует!")
            return

        # Добавление в базу данных
        try:
            with self.db_connection.cursor() as cursor:
                query = """
                INSERT INTO shop (name, address, price_bw, price_cl, password)
                VALUES (%s, %s, %s, %s, %s)
                """
                cursor.execute(query, (
                    fields['name'],
                    fields['address'],
                    fields['bw_price'],
                    fields['color_price'],
                    password_hash
                ))
            self.db_connection.commit()

            self.existing_hashes.add(password_hash)

            for field in ['name', 'address', 'bw_price', 'color_price', 'password']:
                getattr(self, f"{field}_input").clear()

            QMessageBox.information(self, "Успех", "Магазин успешно добавлен!")
            logger.info("Магазин успешно добавлен")
        except pymysql.Error as err:
            self.db_connection.rollback()
            logger.error(f"Ошибка при добавлении магазина: {err}")
            QMessageBox.warning(self, "Ошибка", f"Ошибка при добавлении магазина: {err}")

    def closeEvent(self, event):
        if hasattr(self, 'db_connection'):
            self.db_connection.close()
            logger.info("Соединение с базой данных закрыто")
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