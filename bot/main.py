import asyncio
import logging

from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper  # <-- Исправлено
from bot.settings import settings
from bot.db.session import init_db
from bot.db.models import Base

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

async def main():
    logger.info("🔄 Инициализация базы данных...")
    await init_db(Base)

    logger.info("⚙️ Инициализация OpenAIHelper...")
    openai_helper = OpenAIHelper(settings)

    logger.info("⚙️ Инициализация Telegram-бота...")
    bot = ChatGPTTelegramBot(openai_helper)
    app = await bot.build_app()

    logger.info("✅ Запуск run_polling...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
