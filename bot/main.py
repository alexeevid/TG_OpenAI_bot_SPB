import asyncio
import logging

from telegram.ext import ApplicationBuilder

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db
from bot.db.models import Base

# Настройка логгирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка настроек
settings = load_settings()

async def main():
    logger.info("🔄 Инициализация базы данных...")
    init_db(Base)  # УБРАН await, т.к. функция синхронная

    logger.info("🔧 Инициализация OpenAI Helper...")
    openai = OpenAIHelper(settings)

    logger.info("🤖 Запуск Telegram бота...")
    bot = ChatGPTTelegramBot(openai)

    app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(bot.post_init).build()
    bot.register(app)

    await bot.initialize(app)

    logger.info("🚀 Бот запущен.")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
