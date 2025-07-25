import asyncio
import logging

from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db
from bot.settings import settings
from bot.openai_utils import OpenAIHelper

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

async def main():
    logger.info("🔄 Инициализация базы данных...")
    await init_db()

    logger.info("🤖 Создание OpenAIHelper...")
    openai_helper = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        image_model=settings.image_model
    )

    logger.info("⚙️ Инициализация Telegram-бота...")
    bot = ChatGPTTelegramBot(openai_helper)
    app = await bot.build_app()

    logger.info("✅ Запуск run_polling...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
