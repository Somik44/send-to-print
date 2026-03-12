import asyncio
import os
from datetime import datetime, timedelta

import aiomysql
import uvicorn

from bot import dp, bot
from api import app

UPLOAD_FOLDER = os.path.abspath('uploads')

async def get_db():
    return await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
        autocommit=False,
        cursorclass=aiomysql.DictCursor
    )

async def cleanup_old_orders():
    try:
        async with await get_db() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                three_days_ago = datetime.now() - timedelta(days=3)
                await cursor.execute("""
                    SELECT ID, file_path
                    FROM `order`
                    WHERE status = 'received' AND created_at < %s
                """, (three_days_ago,))
                orders = await cursor.fetchall()

                if not orders:
                    return

                for order in orders:
                    file_path = os.path.join(UPLOAD_FOLDER, order['file_path'])
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except:
                        pass

                ids = [order['ID'] for order in orders]
                placeholders = ','.join(['%s'] * len(ids))
                await cursor.execute(f"""
                    UPDATE `order` SET status = 'canceled'
                    WHERE ID IN ({placeholders})
                """, ids)
                await conn.commit()
    except:
        pass

async def periodic_cleanup():
    while True:
        await cleanup_old_orders()
        await asyncio.sleep(3 * 24 * 60 * 60)

async def start_bot():
    await dp.start_polling(bot)

async def main():
    cleanup_task = asyncio.create_task(periodic_cleanup())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=5000,
        log_level="info"
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        start_bot(),
        cleanup_task
    )

if __name__ == "__main__":
    asyncio.run(main())