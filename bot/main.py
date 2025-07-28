import asyncio
import logging

from telegram.ext import ApplicationBuilder

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db
from bot.db.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = load_settings()

async def main():
    logger.info("🔄 Инициализация базы данных...")
    init_db(Base)

    logger.info("🔧 Инициализация OpenAI Helper...")
    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        image_model=settings.image_model
    )

    logger.info("🤖 Запуск Telegram бота...")
    bot = ChatGPTTelegramBot(openai)

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(bot.post_init)
        .build()
    )

    bot.register(app)
    await bot.initialize(app)

    logger.info("🚀 Бот запущен.")
    await app.run_polling()

if __name__ == "__main__":
    try:
        try:
        asyncio.run(main())
    except RuntimeError as e:
        if "already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise
    except RuntimeError as e:
        if "already running" in str(e):
            import nest_asyncio
            nest_asyncio.apply()
            try:
        asyncio.run(main())
    except RuntimeError as e:
        if "already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise
        else:
            raise