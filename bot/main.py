# bot/main.py
from __future__ import annotations

import logging
from telegram.ext import Application

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def build_application() -> Application:
    settings = load_settings()

    # ВАЖНО: передаём только api_key, чтобы не ловить конфликт сигнатуры.
    openai = OpenAIHelper(
        api_key=settings.openai_api_key
        # Если ваш OpenAIHelper поддерживает другие параметры, можно добавить позже,
        # но сейчас сохраняем совместимость с вашей актуальной версией.
    )

    app = Application.builder().token(settings.telegram_bot_token).build()

    bot = ChatGPTTelegramBot(openai=openai, settings=settings)
    bot.install(app)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # ВАЖНО: без asyncio.run и без await!
    app.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"]
    )


if __name__ == "__main__":
    main()
