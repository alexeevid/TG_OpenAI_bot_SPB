import logging
from telegram.ext import ApplicationBuilder
from bot.config import load_settings
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db
from bot.db.models import Base

def setup_logging(level: str):
    logging.basicConfig(level=level, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

def main():
    settings = load_settings()
    setup_logging(settings.log_level)

    init_db(Base)  # ← именно так!

    oai = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        image_model=settings.image_model,
    )

    bot = ChatGPTTelegramBot(openai_helper=oai)

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot.register(app)
    app.post_init(bot.initialize)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
