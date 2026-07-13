import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from tglol.config import load_config
from tglol.db import init_db
from tglol.handlers import router
from tglol.paths import ensure_storage
from tglol.telegram_service import close_active_sessions


async def main() -> None:
    config = load_config()
    ensure_storage(config)
    init_db(config)
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=config.bot_parse_mode),
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await dp.start_polling(bot, config=config)
    finally:
        await close_active_sessions()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
