import os
import mysql.connector
from mysql.connector import errorcode
from dotenv import load_dotenv
import sys


def create_database(cursor, db_name):
    """Создает базу данных, если она не существует."""
    try:
        cursor.execute(f"CREATE DATABASE {db_name} DEFAULT CHARACTER SET 'utf8mb4'")
        print(f"База данных '{db_name}' создана успешно.")
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DB_CREATE_EXISTS:
            print(f"База данных '{db_name}' уже существует.")
        else:
            print(err.msg)
            sys.exit(1)


def main():
    """
    Основная функция для инициализации базы данных.
    """
    # Загружаем переменные окружения
    env_path = os.path.join(os.path.dirname(__file__), 'config.env')
    load_dotenv(dotenv_path=env_path)

    db_host = os.getenv("DB_HOST")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_name = "send_to_print"

    if not all([db_host, db_user, db_password]):
        print("Ошибка: Переменные DB_HOST, DB_USER, DB_PASSWORD должны быть установлены в config.env")
        return

    try:
        # Подключаемся к серверу MySQL
        cnx = mysql.connector.connect(
            user=db_user,
            password=db_password,
            host=db_host
        )
        cursor = cnx.cursor()
        print("Успешно подключено к MySQL серверу.")

        # Создаем и выбираем базу данных
        create_database(cursor, db_name)
        cursor.execute(f"USE {db_name}")

        # Определяем структуру таблиц
        TABLES = {}

        TABLES['franchise'] = (
            "CREATE TABLE `franchise` ("
            "  `id` int NOT NULL AUTO_INCREMENT,"
            "  `name` varchar(255) NOT NULL,"
            "  `yk_shop_id` varchar(64) NOT NULL,"
            "  `yk_secret_key` varchar(255) NOT NULL,"
            "  `is_active` tinyint(1) DEFAULT '1',"
            "  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,"
            "  PRIMARY KEY (`id`)"
            ") ENGINE=InnoDB"
        )

        TABLES['shop'] = (
            "CREATE TABLE `shop` ("
            "  `ID_shop` int NOT NULL AUTO_INCREMENT,"
            "  `name` varchar(255) DEFAULT NULL,"
            "  `address` varchar(255) NOT NULL,"
            "  `w_hours` varchar(255) NOT NULL,"
            "  `price_bw` decimal(10,2) NOT NULL,"
            "  `price_cl` decimal(10,2) NOT NULL,"
            "  `password` varchar(255) DEFAULT NULL,"
            "  `is_active` tinyint(1) DEFAULT '1',"
            "  `franchise_id` int NOT NULL,"
            "  PRIMARY KEY (`ID_shop`),"
            "  UNIQUE KEY `password` (`password`),"
            "  KEY `franchise_id` (`franchise_id`),"
            "  CONSTRAINT `shop_ibfk_1` FOREIGN KEY (`franchise_id`) "
            "     REFERENCES `franchise` (`id`) ON DELETE CASCADE"
            ") ENGINE=InnoDB"
        )

        TABLES['order'] = (
            "CREATE TABLE `order` ("
            "  `ID` int NOT NULL AUTO_INCREMENT,"
            "  `ID_shop` int NOT NULL,"
            "  `price` decimal(10,2) NOT NULL,"
            "  `note` varchar(255) DEFAULT NULL,"
            "  `con_code` int NOT NULL,"
            "  `color` enum('черно-белая','цветная') NOT NULL,"
            "  `status` enum('created','waiting_payment','paid','in_progress','ready','completed','canceled') "
            "     NOT NULL DEFAULT 'created',"
            "  `file_extension` varchar(10) NOT NULL,"
            "  `file_path` varchar(255) NOT NULL,"
            "  `user_id` varchar(255) NOT NULL,"
            "  `pages` int NOT NULL,"
            "  `payment_id` varchar(64) DEFAULT NULL,"
            "  `payment_status` varchar(32) DEFAULT NULL,"
            "  `paid_at` datetime DEFAULT NULL,"
            "  `payment_amount` decimal(10,2) DEFAULT NULL,"
            "  `idempotence_key` varchar(255) DEFAULT NULL,"
            "  PRIMARY KEY (`ID`),"
            "  UNIQUE KEY `payment_id` (`payment_id`),"
            "  KEY `ID_shop` (`ID_shop`),"
            "  KEY `status` (`status`),"
            "  CONSTRAINT `order_ibfk_1` FOREIGN KEY (`ID_shop`) "
            "     REFERENCES `shop` (`ID_shop`) ON DELETE CASCADE"
            ") ENGINE=InnoDB"
        )

        # Удаляем таблицы в обратном порядке, чтобы избежать ошибок с foreign key
        print("Удаление существующих таблиц (если они есть)...")
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        cursor.execute("DROP TABLE IF EXISTS `order`;")
        cursor.execute("DROP TABLE IF EXISTS `shop`;")
        cursor.execute("DROP TABLE IF EXISTS `franchise`;")
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")

        # Создаем таблицы
        for table_name in ['franchise', 'shop', 'order']:
            table_description = TABLES[table_name]
            try:
                print(f"Создание таблицы '{table_name}': ", end='')
                cursor.execute(table_description)
                print("OK")
            except mysql.connector.Error as err:
                if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
                    print("уже существует.")
                else:
                    print(err.msg)

    except mysql.connector.Error as err:
        print(f"Ошибка подключения к MySQL: {err}")
    finally:
        # Закрываем соединение
        if 'cnx' in locals() and cnx.is_connected():
            cursor.close()
            cnx.close()
            print("Соединение с MySQL закрыто.")


if __name__ == '__main__':
    print("Запуск скрипта инициализации базы данных...")
    main()
    print("Скрипт завершил работу.")