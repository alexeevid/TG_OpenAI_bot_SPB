# bot/main.py
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application

from bot.config import load_settings
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot


logger = logging.getLogger(__name__)


def build_application() -> Application:
    settings = load_settings()

    # Создаём OpenAI helper с новыми именами параметров.
    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=getattr(settings, "openai_model", None),
        image_model=getattr(settings, "image_model", None),  # вместо image_primary
        temperature=getattr(settings, "openai_temperature", 0.2),
        enable_image_generation=bool(getattr(settings, "enable_image_generation", True)),
    )

    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )

    bot.install(app)
    return app


def main():
    logging.basicConfig(
        level=getattr(logging, "INFO", logging.INFO),
        format="%(levelname)s:%(name)s:%(message)s",
    )
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")
    # allowed_updates=Update.ALL_TYPES — чтобы обрабатывать voice/photo/docs
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
