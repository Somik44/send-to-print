import sys
import urllib3
import telebot
import logging
import os
from PyQt5 import QtWidgets
from PyQt5.QtWidgets import QVBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QWidget, QHBoxLayout, QMessageBox
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
import subprocess
import random
import pythoncom
import win32com.client
from PyPDF2 import PdfReader
import textwrap

API_TOKEN = '7818669005:AAFyAMagVNx7EfJsK-pVLUBkGLfmMp9J2EQ'
bot = telebot.TeleBot(API_TOKEN)
user_states = {}
order_codes = {}

def read_order_number():
    try:
        with open('order_number', 'r') as f:
            return int(f.read())
    except FileNotFoundError:
        return 1

def write_order_number(order_number):
    with open('order_number', 'w') as f:
        f.write(str(order_number))

order_number = read_order_number()

class BotThread(QThread):
    message_received = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.ready_to_receive_files = False

    def run(self):
        @bot.message_handler(commands=['start'])
        def send_welcome(message):
            bot.send_message(message.chat.id, f"Привет, {message.from_user.first_name}! Рады приветствовать тебя на нашем сервисе по распечатке документов в любое удобное время! Чтобы начать новый заказ, используйте команду /new_order.")

        @bot.message_handler(commands=['new_order'])
        def new_order(message):
            bot.send_message(message.chat.id, "Отправь только один PDF, DOCX или DOC файл(не более 20 МБ), а бот рассчитает его стоимость.")
            bot.send_message(message.chat.id, "Прайс лист: 5 рублей/страница - Черно-белый формат; 15 рублей/страница - Цветной формат")
            user_states[message.chat.id] = 'awaiting_file'
            self.ready_to_receive_files = True

        @bot.message_handler(commands=['reset'])
        def reset(message):
            user_states.pop(message.chat.id, None)
            bot.send_message(message.chat.id, "Состояние сброшено. Вы можете начать новый заказ, используя команду /new_order.")

        @bot.message_handler(content_types=['document'])
        def handle_document(message):
            try:
                if not self.ready_to_receive_files:
                    bot.reply_to(message, "Сначала нажмите /new_order, чтобы начать прием файлов.")
                    return

                if message.document.mime_type not in [
                    'application/pdf',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'application/msword'
                ]:
                    bot.reply_to(message, "Неподдерживаемый формат документа. Пожалуйста, отправьте PDF, DOCX или DOC.")
                    return

                # Генерация данных заказа
                global order_number
                code_order = random.randint(10000, 99999)
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)

                # Обработка имени файла
                original_name = message.document.file_name
                name, extension = os.path.splitext(original_name)
                final_file_name = f"order_{order_number}{extension}"
                final_file_path = os.path.join(os.getcwd(), final_file_name)  # Используем текущую директорию

                # Сохранение файла
                with open(final_file_path, 'wb') as new_file:
                    new_file.write(downloaded_file)

                # Подсчет страниц
                pages = None
                if extension.lower() == '.pdf':
                    pages = get_pdf_page_count(final_file_path)
                elif extension.lower() in ('.docx', '.doc'):
                    pages = get_doc_page_count(final_file_path)

                if not pages:
                    bot.reply_to(message, "Ошибка обработки файла")
                    os.remove(final_file_path)
                    return

                # Подготовка данных заказа
                order_data = {
                    'user_id': message.chat.id,
                    'file_path': final_file_name,
                    'check_number': code_order,
                    'pages': pages,
                    'extension': extension,
                }

                # Создание клавиатуры для выбора цвета
                markup = telebot.types.ReplyKeyboardMarkup(
                    one_time_keyboard=True,
                    resize_keyboard=True
                )
                markup.row('Черно-белая', 'Цветная')

                bot.send_message(
                    message.chat.id,
                    f"Количество страниц: {pages}\n"
                    "Выберите тип печати:",
                    reply_markup=markup
                )

                bot.register_next_step_handler(
                    message,
                    lambda msg: process_color_selection(msg, order_data)
                )

            except Exception as e:
                logging.error(f"Document handler error: {e}")
                bot.reply_to(message, "Произошла ошибка при обработке файла")

        def process_color_selection(message, order_data):
            try:
                remove_keyboard = telebot.types.ReplyKeyboardRemove()
                color_choice = message.text.lower()

                if 'черно-белая' in color_choice:
                    order_data['color'] = 'черно-белая'
                    order_data['cost'] = order_data['pages'] * 10
                elif 'цветная' in color_choice:
                    order_data['color'] = 'цветная'
                    order_data['cost'] = order_data['pages'] * 15
                else:
                    raise ValueError("Некорректный выбор цвета")

                # Запрос комментария к заказу
                bot.send_message(message.chat.id, "Комментарий к заказу (поставьте $ если комментарий не нужен):")
                bot.register_next_step_handler(message, lambda msg: process_comment(msg, order_data))

            except Exception as e:
                logging.error(f"Color selection error: {e}")
                bot.send_message(message.chat.id, "Ошибка выбора цвета, начните заново",
                                 reply_markup=telebot.types.ReplyKeyboardRemove())
                file_path = os.path.join(os.getcwd(), order_data['file_path'])
                if os.path.exists(file_path):
                    os.remove(file_path)

        def process_comment(message, order_data):
            try:
                comment = message.text.strip()
                if comment != '$':
                    order_data['comment'] = comment
                else:
                    order_data['comment'] = None  # Нет комментария

                markup = telebot.types.ReplyKeyboardMarkup(
                    one_time_keyboard=True,
                    resize_keyboard=True
                )
                markup.row('Подтвердить', 'Отменить')

                bot.send_message(
                    message.chat.id,
                    f"Страниц: {order_data['pages']}\n"
                    f"Цвет: {order_data['color']}\n"
                    f"Сумма: {order_data['cost']} руб.\n"
                    f"Комментарий: {order_data.get('comment', 'Нет комментария')}\n\n"
                    "Подтвердите заказ:",
                    reply_markup=markup
                )

                bot.register_next_step_handler(
                    message,
                    lambda msg: handle_confirmation(msg, order_data)
                )

            except Exception as e:
                logging.error(f"Comment processing error: {e}")
                bot.send_message(message.chat.id, "Ошибка обработки комментария",
                                 reply_markup=telebot.types.ReplyKeyboardRemove())
                file_path = os.path.join(os.getcwd(), order_data['file_path'])
                if os.path.exists(file_path):
                    os.remove(file_path)

        def print_file(self, file_name):
            order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
            if not order_info:
                QtWidgets.QMessageBox.warning(self, "Ошибка", "Информация о заказе не найдена.")
                return

            file_path = os.path.join(os.getcwd(), file_name)
            if not os.path.isfile(file_path):
                QtWidgets.QMessageBox.warning(self, "Ошибка", f"Файл не найден: {file_path}")
                return

            comment = order_info.get('comment', 'Нет комментария')
            try:
                if file_name.lower().endswith('.pdf'):
                    subprocess.run(
                        ["C:\\Users\\petry\\AppData\\Local\\Yandex\\YandexBrowser\\Application\\browser.exe", "/p",
                         file_path])
                elif file_name.lower().endswith(('.docx', '.doc')):
                    subprocess.run(
                        ["C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE", "/p", file_path])

                # Отображение комментария перед печатью
                if comment:
                    QtWidgets.QMessageBox.information(self, "Комментарий к заказу", f"Комментарий: {comment}")

            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось напечатать файл: {str(e)}")

        def handle_confirmation(message, order_data):
            try:
                remove_keyboard = telebot.types.ReplyKeyboardRemove()
                choice = message.text.lower()

                if 'подтвердить' in choice:
                    global order_number
                    order_codes[order_number] = order_data
                    bot.send_message(
                        message.chat.id,
                        f"Заказ №{order_number} принят!\n"
                        f"Код для получения: {order_data['check_number']}",
                        reply_markup=remove_keyboard
                    )
                    order_number += 1
                    write_order_number(order_number)
                    self.message_received.emit(order_data['file_path'])
                elif 'отменить' in choice:
                    file_path = os.path.join(os.getcwd(), order_data['file_path'])
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    bot.send_message(
                        message.chat.id,
                        "Заказ отменен",
                        reply_markup=remove_keyboard
                    )
                else:
                    raise ValueError("Некорректный выбор подтверждения")

            except Exception as e:
                logging.error(f"Confirmation error: {e}")
                bot.send_message(
                    message.chat.id,
                    "Ошибка обработки подтверждения",
                    reply_markup=remove_keyboard
                )
                file_path = os.path.join(os.getcwd(), order_data['file_path'])
                if os.path.exists(file_path):
                    os.remove(file_path)

        def get_pdf_page_count(file_path):
            try:
                with open(file_path, 'rb') as file:
                    reader = PdfReader(file)
                    return len(reader.pages)
            except Exception as e:
                logging.error(f"Ошибка при подсчете страниц в PDF файле: {e}")
                return None

        def get_docx_page_count(file_path):
            try:
                pythoncom.CoInitialize()
                word = win32com.client.Dispatch("Word.Application")
                word.Visible = False
                doc = word.Documents.Open(file_path)
                page_count = doc.ComputeStatistics(2)
                doc.Close(False)
                return page_count
            except Exception as e:
                logging.error(f"Ошибка при подсчете страниц в DOCX файле: {e}")
                return None
            finally:
                word.Quit()
                pythoncom.CoUninitialize()

        def get_doc_page_count(file_path):
            pythoncom.CoInitialize()
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            try:
                doc = word.Documents.Open(file_path)
                page_count = doc.ComputeStatistics(2)
                doc.Close(False)
                return page_count
            except Exception as e:
                logging.error(f"Ошибка при подсчете страниц: {e}")
                return None
            finally:
                word.Quit()
                pythoncom.CoUninitialize()

        bot.polling(none_stop=True)


class FileReceiverApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.start_bot()

    def initUI(self):
        self.setWindowIcon(QIcon("D:\\проект Отправь на печать и забери!\\logo.png"))
        self.setWindowTitle('Send to print and pick up!')
        self.setFixedSize(1000, 800)

        self.layout = QVBoxLayout()
        self.received_label = QLabel('Полученные файлы:')
        self.layout.addWidget(self.received_label)
        self.received_list = QListWidget()
        self.layout.addWidget(self.received_list)

        self.ready_label = QLabel('Готовые к выдаче файлы:')
        self.layout.addWidget(self.ready_label)
        self.ready_list = QListWidget()
        self.layout.addWidget(self.ready_list)

        self.setLayout(self.layout)
        font = QFont("San Francisco", 10)
        self.received_label.setFont(font)
        self.ready_label.setFont(font)
        self.received_list.setFont(font)
        self.ready_list.setFont(font)

    def start_bot(self):
        self.bot_thread = BotThread()
        self.bot_thread.message_received.connect(self.update_received_list)
        self.bot_thread.start()

    def update_received_list(self, file_name):
        item = QListWidgetItem()
        h_layout = QHBoxLayout()
        label = QLabel(f"Получен файл: {file_name}")
        h_layout.addWidget(label)
        label.setStyleSheet('color: red')
        label.setFont(QFont("Arial", 12))

        button = QPushButton('Готов к выдаче')
        button.clicked.connect(lambda: self.move_to_ready(file_name, item))
        h_layout.addWidget(button)
        button.setFixedSize(180, 30)

        info_button = QPushButton('Подробная информация')
        info_button.clicked.connect(lambda: self.show_file_info(file_name))
        h_layout.addWidget(info_button)
        info_button.setFixedSize(220, 30)

        widget = QWidget()
        widget.setLayout(h_layout)
        widget.setMinimumHeight(50)
        widget.setMaximumHeight(50)

        self.received_list.addItem(item)
        self.received_list.setItemWidget(item, widget)
        self.received_list.setSpacing(10)

    def show_file_info(self, file_name):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        info_text = "\n".join([
            f"Имя файла: {file_name}",
            f"Цвет печати: {order_info.get('color', 'Неизвестно')}",
            f"Количество страниц: {order_info.get('pages', 'Неизвестно')}",
            f"Стоимость: {order_info.get('cost', 'Неизвестно')} руб.",
            f"Проверочный код: {order_info.get('check_number', 'Неизвестно')}",
            f"Комментарий: {order_info.get('comment', 'Нет комментария')}"
        ])
        QMessageBox.information(self, "Подробная информация о файле", info_text)

    def move_to_ready(self, file_name, item):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if order_info:
            bot.send_message(order_info['user_id'], f"Ваш заказ изготовлен. Можете его забрать.")

        row = self.received_list.row(item)
        self.received_list.takeItem(row)

        ready_item = QListWidgetItem()
        h_layout = QHBoxLayout()
        label = QLabel(f"Готов к выдаче: {file_name}")
        label.setStyleSheet('color: green')
        label.setFont(QFont("Arial", 12))
        h_layout.addWidget(label)

        print_button = QPushButton('Печать')
        print_button.clicked.connect(lambda: self.print_file(file_name))
        h_layout.addWidget(print_button)
        print_button.setFixedSize(100, 30)

        show_code_button = QPushButton('Показать код выдачи')
        show_code_button.clicked.connect(lambda: self.show_code(file_name))
        h_layout.addWidget(show_code_button)
        show_code_button.setFixedSize(185, 30)

        ready_widget = QWidget()
        ready_widget.setLayout(h_layout)
        ready_widget.setMinimumHeight(50)
        ready_widget.setMaximumHeight(50)

        self.ready_list.insertItem(0, ready_item)
        self.ready_list.setItemWidget(ready_item, ready_widget)
        self.ready_list.setSpacing(10)

    def show_code(self, file_name):
        order_info = next((v for v in order_codes.values() if v['file_path'] == file_name), {})
        if not order_info:
            return

        msg_box = QMessageBox()
        msg_box.setWindowTitle("Код заказа")
        msg_box.setText(f"Код данного заказа: {order_info['check_number']}")
        msg_box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

        if msg_box.exec_() == QMessageBox.Ok:
            file_path = os.path.join(os.getcwd(), file_name)
            try:
                os.remove(file_path)
                QMessageBox.information(self, "Успех", f"Файл {file_name} был успешно удален.")
                bot.send_message(order_info['user_id'],"Заказ должен быть у вас. Спасибо, что воспользовались нашими услугами!")
            except Exception as e:
                print(f"Не удалось удалить файл: {str(e)}")

    def print_file(self, file_name):
        file_path = os.path.join(os.getcwd(), file_name)
        if not os.path.isfile(file_path):
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Файл не найден: {file_path}")
            return

        try:
            if file_name.lower().endswith('.pdf'):
                subprocess.run(["C:\\Users\\petry\\AppData\\Local\\Yandex\\YandexBrowser\\Application\\browser.exe", "/p", file_path])
            elif file_name.lower().endswith(('.docx', '.doc')):
                subprocess.run(["C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE", "/p", file_path])
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Не удалось напечатать файл: {str(e)}")


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    receiver = FileReceiverApp()
    receiver.show()
    sys.exit(app.exec_())