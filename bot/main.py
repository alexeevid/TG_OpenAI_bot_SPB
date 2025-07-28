import logging
from telegram.ext import ApplicationBuilder

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db
from bot.db.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def build_application():
    settings = load_settings()
    init_db(Base)
    openai = OpenAIHelper(api_key=settings.openai_api_key, default_model=settings.openai_model)
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot.install(app)
    return app

def main():
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
