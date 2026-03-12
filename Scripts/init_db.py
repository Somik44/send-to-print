import asyncio
import aiomysql
import os

async def init_db():
    conn = await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        autocommit=True
    )

    async with conn.cursor() as cursor:
        # Создание базы данных, если её нет
        await cursor.execute("CREATE DATABASE IF NOT EXISTS unn CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        await cursor.execute("USE unn;")

        # Таблица shop (без изменений)
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop (
                ID_shop INT NOT NULL AUTO_INCREMENT,
                name VARCHAR(255) NOT NULL,
                address TEXT,
                w_hours VARCHAR(255),
                password VARCHAR(255) NOT NULL,
                PRIMARY KEY (ID_shop)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

        # Таблица order с добавленными полями: created_at и расширенный ENUM статуса
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS `order` (
                ID INT NOT NULL AUTO_INCREMENT,
                ID_shop INT NOT NULL,
                user_id BIGINT NOT NULL,
                file_path VARCHAR(255) NOT NULL,
                user_file_name VARCHAR(255) NOT NULL,
                status ENUM('received','completed','canceled') DEFAULT 'received',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ID),
                INDEX idx_user_status (user_id, status),
                CONSTRAINT fk_order_shop
                    FOREIGN KEY (ID_shop)
                    REFERENCES shop(ID_shop)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)

    conn.close()
    print("✅ База данных успешно инициализирована!")

if __name__ == "__main__":
    asyncio.run(init_db())