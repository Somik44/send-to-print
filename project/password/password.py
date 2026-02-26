import sys
import requests
import hashlib
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QLabel, QLineEdit, QPushButton, QMessageBox,
                             QComboBox, QHBoxLayout, QGroupBox)
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
        self.setWindowTitle("Управление точками и франшизами")
        self.setGeometry(100, 100, 450, 400)
        self.current_shop_data = None
        self.franchises = []
        self.shops = []
        self.initUI()
        self.fetch_franchises()

    def initUI(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout()

        # Выбор режима
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Режим:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Добавить новую точку", "Изменить точку", "Создать франшизу"])
        self.mode_combo.currentIndexChanged.connect(self.switch_mode)
        mode_layout.addWidget(self.mode_combo)
        main_layout.addLayout(mode_layout)

        # Карточка для динамического содержимого
        self.card = QGroupBox()
        self.card_layout = QVBoxLayout()
        self.card.setLayout(self.card_layout)
        main_layout.addWidget(self.card)

        # Кнопка отправки
        self.submit_button = QPushButton("Выполнить")
        self.submit_button.clicked.connect(self.on_submit)
        main_layout.addWidget(self.submit_button)

        central_widget.setLayout(main_layout)

        self.switch_mode(0)

    # ---------- Работа с данными ----------
    def fetch_franchises(self):
        """Загружает список франшиз и обновляет комбобокс, если нужно."""
        try:
            headers = {"X-Admin-Key": ADMIN_API_KEY}
            response = requests.get(f"{API_URL}/admin/franchise", headers=headers, timeout=10)
            if response.status_code == 200:
                self.franchises = response.json()
                logger.info("Franchises loaded")
                # Обновляем комбобокс, если мы в режиме добавления/изменения
                if self.mode_combo.currentIndex() in (0, 1) and hasattr(self, 'franchise_combo'):
                    self.update_franchise_combo()
            else:
                logger.error(f"Failed to load franchises: {response.status_code}")
                QMessageBox.warning(self, "Ошибка", "Не удалось загрузить список франшиз")
        except Exception as e:
            logger.exception("Error loading franchises")
            QMessageBox.critical(self, "Ошибка", f"Ошибка загрузки франшиз: {str(e)}")

    def fetch_shops(self):
        """Загружает список магазинов и обновляет комбобокс, если нужно."""
        try:
            headers = {"X-Admin-Key": ADMIN_API_KEY}
            response = requests.get(f"{API_URL}/admin/shops", headers=headers, timeout=10)
            if response.status_code == 200:
                self.shops = response.json()
                logger.info("Shops fetched")
                # Обновляем комбобокс магазинов только в режиме изменения
                if self.mode_combo.currentIndex() == 1 and hasattr(self, 'shop_combo'):
                    self.update_shops_combo()
            else:
                logger.error(f"Failed to fetch shops: {response.status_code}")
                QMessageBox.warning(self, "Ошибка", "Не удалось загрузить список магазинов")
        except Exception as e:
            logger.exception("Error fetching shops")
            QMessageBox.critical(self, "Ошибка", f"Ошибка загрузки магазинов: {str(e)}")

    def update_franchise_combo(self):
        if not hasattr(self, 'franchise_combo') or self.franchise_combo is None:
            return
        if self.mode_combo.currentIndex() not in (0, 1):
            return
        try:
            self.franchise_combo.blockSignals(True)
            self.franchise_combo.clear()
            if not self.franchises:
                self.franchise_combo.addItem("Нет доступных франшиз", None)
            else:
                for f in self.franchises:
                    self.franchise_combo.addItem(f['name'], f['id'])
        finally:
            self.franchise_combo.blockSignals(False)

    def update_shops_combo(self):
        if not hasattr(self, 'shop_combo') or self.shop_combo is None:
            return
        if self.mode_combo.currentIndex() != 1:
            return
        try:
            self.shop_combo.blockSignals(True)  # блокируем сигналы на время обновления
            current_data = self.shop_combo.currentData()
            self.shop_combo.clear()
            self.shop_combo.addItem("-- Выберите магазин --", None)
            for shop in self.shops:
                display_text = f"{shop['address']} ({shop['name']})"
                self.shop_combo.addItem(display_text, shop['ID_shop'])
            if current_data is not None:
                index = self.shop_combo.findData(current_data)
                if index >= 0:
                    self.shop_combo.setCurrentIndex(index)
        finally:
            self.shop_combo.blockSignals(False)  # восстанавливаем сигналы

    # ---------- Управление интерфейсом ----------
    def clear_card(self):
        """Удаляет все виджеты из карточки."""
        for i in reversed(range(self.card_layout.count())):
            widget = self.card_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

    def switch_mode(self, index):
        """Переключает интерфейс в зависимости от выбранного режима."""
        self.clear_card()
        self.current_shop_data = None

        if index == 0:
            self.setup_add_shop_ui()
        elif index == 1:
            self.setup_edit_shop_ui()
        elif index == 2:
            self.setup_add_franchise_ui()

    def setup_add_shop_ui(self):
        """Интерфейс для добавления новой точки."""
        self.name_input = QLineEdit()
        self.address_input = QLineEdit()
        self.w_hours_input = QLineEdit()
        self.bw_price_input = QLineEdit()
        self.color_price_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)

        self.franchise_combo = QComboBox()
        self.update_franchise_combo()

        fields = [
            ("Название магазина:", self.name_input),
            ("Адрес:", self.address_input),
            ("Часы работы:", self.w_hours_input),
            ("Цена ч/б:", self.bw_price_input),
            ("Цена цветная:", self.color_price_input),
            ("Пароль:", self.password_input),
            ("Франшиза:", self.franchise_combo),
        ]

        for label_text, field in fields:
            self.card_layout.addWidget(QLabel(label_text))
            self.card_layout.addWidget(field)

        self.submit_button.setText("Добавить магазин")

    def setup_edit_shop_ui(self):
        """Интерфейс для изменения существующей точки."""
        # Выбор магазина
        self.card_layout.addWidget(QLabel("Выберите магазин:"))
        self.shop_combo = QComboBox()
        self.shop_combo.currentIndexChanged.connect(self.on_shop_selected)
        self.card_layout.addWidget(self.shop_combo)

        # Поля (будут заполнены после выбора) – создаём ДО загрузки списка магазинов
        self.name_input = QLineEdit()
        self.address_input = QLineEdit()
        self.w_hours_input = QLineEdit()
        self.bw_price_input = QLineEdit()
        self.color_price_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Оставьте пустым, если не меняете")
        self.password_input.setEchoMode(QLineEdit.Password)

        self.franchise_combo = QComboBox()
        self.update_franchise_combo()  # заполняем список франшиз

        self.fields = [
            ("Название:", self.name_input),
            ("Адрес:", self.address_input),
            ("Часы работы:", self.w_hours_input),
            ("Цена ч/б:", self.bw_price_input),
            ("Цена цветная:", self.color_price_input),
            ("Пароль (новый):", self.password_input),
            ("Франшиза:", self.franchise_combo),
        ]

        self.edit_labels = []
        for label_text, field in self.fields:
            label = QLabel(label_text)
            label.setVisible(False)
            field.setVisible(False)
            self.card_layout.addWidget(label)
            self.card_layout.addWidget(field)
            self.edit_labels.append(label)

        # ---- КНОПКА ВКЛ/ВЫКЛ ----
        self.toggle_button = QPushButton()
        self.toggle_button.clicked.connect(self.toggle_shop_status)
        self.toggle_button.setVisible(False)
        self.card_layout.addWidget(self.toggle_button)

        # Теперь загружаем магазины – сигнал сработает, но поля уже существуют
        self.fetch_shops()

        self.submit_button.setText("Сохранить изменения")
        self.submit_button.setEnabled(False)

    def setup_add_franchise_ui(self):
        """Интерфейс для создания новой франшизы."""
        self.franchise_name_input = QLineEdit()
        self.yk_shop_id_input = QLineEdit()
        self.yk_secret_key_input = QLineEdit()

        fields = [
            ("Название франшизы:", self.franchise_name_input),
            ("YooKassa Shop ID:", self.yk_shop_id_input),
            ("YooKassa Secret Key:", self.yk_secret_key_input),
        ]

        for label_text, field in fields:
            self.card_layout.addWidget(QLabel(label_text))
            self.card_layout.addWidget(field)

        self.submit_button.setText("Создать франшизу")

    # ---------- Обработчики ----------
    def on_shop_selected(self, index):
        """Загружает данные выбранного магазина."""
        # Если режим изменился, игнорируем сигнал
        if self.mode_combo.currentIndex() != 1:
            return

        shop_id = self.shop_combo.currentData()
        if shop_id is None:
            # Скрываем поля с защитой от удалённых виджетов
            try:
                for label, field in zip(self.edit_labels, [f[1] for f in self.fields]):
                    label.setVisible(False)
                    field.setVisible(False)
                self.submit_button.setEnabled(False)
                if hasattr(self, "toggle_button"):
                    self.toggle_button.setVisible(False)
            except RuntimeError:
                pass
            return

        try:
            headers = {"X-Admin-Key": ADMIN_API_KEY}
            response = requests.get(f"{API_URL}/admin/shops/{shop_id}", headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                self.current_shop_data = data
                is_active = data.get("is_active", 1)

                if is_active == 1:
                    self.toggle_button.setText("Выключить точку")
                else:
                    self.toggle_button.setText("Включить точку")

                self.toggle_button.setVisible(True)

                # Заполняем поля (проверяем, что виджеты ещё существуют)
                try:
                    self.name_input.setText(data['name'])
                    self.address_input.setText(data['address'])
                    self.w_hours_input.setText(data['w_hours'])
                    self.bw_price_input.setText(str(data['price_bw']))
                    self.color_price_input.setText(str(data['price_cl']))
                    self.password_input.clear()

                    # Выбираем франшизу
                    idx = self.franchise_combo.findData(data['franchise_id'])
                    if idx >= 0:
                        self.franchise_combo.setCurrentIndex(idx)

                    # Показываем поля
                    for label, field in zip(self.edit_labels, [f[1] for f in self.fields]):
                        label.setVisible(True)
                        field.setVisible(True)
                    self.submit_button.setEnabled(True)
                except RuntimeError:
                    # Если какой-то виджет уже удалён – выходим
                    return
            else:
                QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить данные магазина: {response.status_code}")
        except Exception as e:
            logger.exception("Error loading shop data")
            QMessageBox.critical(self, "Ошибка", f"Ошибка загрузки данных: {str(e)}")

    def on_submit(self):
        """Общий обработчик кнопки."""
        mode = self.mode_combo.currentIndex()
        if mode == 0:
            self.add_shop()
        elif mode == 1:
            self.update_shop()
        elif mode == 2:
            self.add_franchise()

    def add_shop(self):
        """Добавление новой точки."""
        name = self.name_input.text().strip()
        address = self.address_input.text().strip()
        w_hours = self.w_hours_input.text().strip()
        bw_price = self.bw_price_input.text().strip()
        color_price = self.color_price_input.text().strip()
        password = self.password_input.text().strip()
        franchise_id = self.franchise_combo.currentData()

        if not all([name, address, w_hours, bw_price, color_price, password]):
            QMessageBox.warning(self, "Ошибка", "Все поля должны быть заполнены!")
            return
        if franchise_id is None:
            QMessageBox.warning(self, "Ошибка", "Выберите франшизу!")
            return

        try:
            bw_price = float(bw_price)
            color_price = float(color_price)
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Цены должны быть числовыми!")
            return

        password_hash = hashlib.sha256(password.encode()).hexdigest()

        payload = {
            "name": name,
            "address": address,
            "w_hours": w_hours,
            "price_bw": bw_price,
            "price_cl": color_price,
            "password": password_hash,
            "franchise_id": franchise_id
        }

        try:
            headers = {"Content-Type": "application/json", "X-Admin-Key": ADMIN_API_KEY}
            response = requests.post(f"{API_URL}/admin/shops", json=payload, timeout=10, headers=headers)

            if response.status_code == 201:
                self.name_input.clear()
                self.address_input.clear()
                self.w_hours_input.clear()
                self.bw_price_input.clear()
                self.color_price_input.clear()
                self.password_input.clear()
                QMessageBox.information(self, "Успех", "Магазин успешно добавлен!")
                self.fetch_shops()
                if self.mode_combo.currentIndex() == 1:
                    self.update_shops_combo()
            elif response.status_code == 409:
                QMessageBox.warning(self, "Ошибка", "Магазин с таким паролем уже существует")
            else:
                error_msg = response.json().get("detail", "Неизвестная ошибка") if response.text else "Ошибка сервера"
                QMessageBox.critical(self, "Ошибка", f"Код {response.status_code}: {error_msg}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось отправить запрос: {str(e)}")

    def update_shop(self):
        """Обновление существующей точки."""
        if not self.current_shop_data:
            QMessageBox.warning(self, "Ошибка", "Сначала выберите магазин")
            return

        shop_id = self.current_shop_data['ID_shop']
        payload = {}

        name = self.name_input.text().strip()
        if name and name != self.current_shop_data['name']:
            payload['name'] = name

        address = self.address_input.text().strip()
        if address and address != self.current_shop_data['address']:
            payload['address'] = address

        w_hours = self.w_hours_input.text().strip()
        if w_hours and w_hours != self.current_shop_data['w_hours']:
            payload['w_hours'] = w_hours

        try:
            bw_price = float(self.bw_price_input.text().strip()) if self.bw_price_input.text().strip() else None
            if bw_price is not None and bw_price != self.current_shop_data['price_bw']:
                payload['price_bw'] = bw_price
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Цена ч/б должна быть числом")
            return

        try:
            color_price = float(self.color_price_input.text().strip()) if self.color_price_input.text().strip() else None
            if color_price is not None and color_price != self.current_shop_data['price_cl']:
                payload['price_cl'] = color_price
        except ValueError:
            QMessageBox.warning(self, "Ошибка", "Цена цветная должна быть числом")
            return

        password = self.password_input.text().strip()
        if password:
            payload['password'] = hashlib.sha256(password.encode()).hexdigest()

        franchise_id = self.franchise_combo.currentData()
        if franchise_id is not None and franchise_id != self.current_shop_data['franchise_id']:
            payload['franchise_id'] = franchise_id

        if not payload:
            QMessageBox.information(self, "Информация", "Нет изменений для сохранения")
            return

        try:
            headers = {"Content-Type": "application/json", "X-Admin-Key": ADMIN_API_KEY}
            response = requests.patch(f"{API_URL}/admin/shops/{shop_id}", json=payload, timeout=10, headers=headers)

            if response.status_code == 200:
                QMessageBox.information(self, "Успех", "Данные магазина обновлены")
                self.current_shop_data.update(payload)
                self.fetch_shops()
                if hasattr(self, 'shop_combo') and self.mode_combo.currentIndex() == 1:
                    self.update_shops_combo()
            else:
                error_msg = response.json().get("detail", "Неизвестная ошибка") if response.text else "Ошибка сервера"
                QMessageBox.critical(self, "Ошибка", f"Код {response.status_code}: {error_msg}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось отправить запрос: {str(e)}")

    def add_franchise(self):
        """Создание новой франшизы."""
        name = self.franchise_name_input.text().strip()
        yk_shop_id = self.yk_shop_id_input.text().strip()
        yk_secret_key = self.yk_secret_key_input.text().strip()

        if not all([name, yk_shop_id, yk_secret_key]):
            QMessageBox.warning(self, "Ошибка", "Все поля должны быть заполнены!")
            return

        payload = {
            "name": name,
            "yk_shop_id": yk_shop_id,
            "yk_secret_key": yk_secret_key
        }

        try:
            headers = {"Content-Type": "application/json", "X-Admin-Key": ADMIN_API_KEY}
            response = requests.post(f"{API_URL}/admin/franchise", json=payload, timeout=10, headers=headers)

            if response.status_code == 201:
                self.franchise_name_input.clear()
                self.yk_shop_id_input.clear()
                self.yk_secret_key_input.clear()
                QMessageBox.information(self, "Успех", "Франшиза успешно создана!")
                self.fetch_franchises()
            else:
                error_msg = response.json().get("detail", "Неизвестная ошибка") if response.text else "Ошибка сервера"
                QMessageBox.critical(self, "Ошибка", f"Код {response.status_code}: {error_msg}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось отправить запрос: {str(e)}")

    def toggle_shop_status(self):
        if not self.current_shop_data:
            return

        shop_id = self.current_shop_data['ID_shop']
        current_status = self.current_shop_data.get("is_active", 1)

        new_status = 0 if current_status == 1 else 1
        action_text = "выключить" if current_status == 1 else "включить"

        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Вы уверены, что хотите {action_text} эту точку?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        try:
            headers = {
                "Content-Type": "application/json",
                "X-Admin-Key": ADMIN_API_KEY
            }

            response = requests.patch(
                f"{API_URL}/admin/shops/{shop_id}",
                json={"is_active": new_status},
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                self.current_shop_data["is_active"] = new_status

                if new_status == 1:
                    self.toggle_button.setText("Выключить точку")
                    QMessageBox.information(self, "Успех", "Точка включена")
                else:
                    self.toggle_button.setText("Включить точку")
                    QMessageBox.information(self, "Успех", "Точка выключена")

            else:
                error_msg = response.json().get("detail", "Ошибка сервера") if response.text else "Ошибка"
                QMessageBox.critical(self, "Ошибка", error_msg)

        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        window = ShopApp()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        QMessageBox.critical(None, "Ошибка", f"Критическая ошибка: {e}")