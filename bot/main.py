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

    # Инициализируем OpenAI-хелпер (совместимо с вашим кодом/параметрами)
    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=getattr(settings, "openai_model", None),
        image_model=getattr(settings, "image_model", None),
        temperature=getattr(settings, "openai_temperature", 0.2),
        enable_image_generation=bool(getattr(settings, "enable_image_generation", True)),
    )

    # Собираем приложение Telegram
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Подключаем бота (регистрация всех handlers)
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
