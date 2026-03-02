import asyncio
import os
import uvicorn

from bot import dp, bot
from api import app


async def start_bot():
    await dp.start_polling(bot)


async def main():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=5000,
        log_level="info"
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        start_bot()
    )


if __name__ == "__main__":
    asyncio.run(main())