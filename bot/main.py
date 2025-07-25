import asyncio
import logging

from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db
from bot.settings import settings

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

bot = ChatGPTTelegramBot()

async def main():
    logger.info("🔄 Инициализация базы данных...")
    await init_db()

    logger.info("⚙️ Инициализация Telegram-бота...")
    app = await bot.build_app()

    logger.info("✅ Запуск run_polling...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
