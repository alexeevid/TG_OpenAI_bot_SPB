import logging
from telegram.ext import ApplicationBuilder
from bot.config import load_settings
from bot.error_tracer import init_error_tracer
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import engine, Base

def setup_logging(level: str):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

def main():
    settings = load_settings()
    setup_logging(settings.log_level)
    init_error_tracer(None)

    if engine is not None:
        Base.metadata.create_all(engine)

    openai_helper = OpenAIHelper(config={
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
        "image_model": settings.image_model
    })

    bot = ChatGPTTelegramBot(
        token=settings.telegram_bot_token,
        openai_helper=openai_helper,
        yandex_token=settings.yandex_disk_token,
        yandex_root=settings.yandex_root_path,
        admins=settings.admin_user_ids,
        allowed=settings.allowed_telegram_user_ids
    )

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    bot.register(application)
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
