from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import Application

from bot.config import load_settings
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db

logger = logging.getLogger(__name__)


def build_application() -> Application:
    settings = load_settings()

    # Инициализируем БД и таблицы
    init_db()

    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        default_model=getattr(settings, "openai_model", "gpt-4o"),
        default_temperature=getattr(settings, "openai_temperature", 0.2),
        image_primary=(getattr(settings, "image_model", None) or "gpt-image-1"),
        # при желании можно добавить fallback-модель из настроек:
        # image_fallback=getattr(settings, "image_fallback_model", "dall-e-3"),
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
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
