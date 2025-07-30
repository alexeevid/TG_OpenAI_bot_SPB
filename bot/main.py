# bot/main.py
from __future__ import annotations

import logging
from telegram.ext import Application

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

# приглушаем шум
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
logging.getLogger("yadisk").setLevel(logging.WARNING)

# Если нужно скрыть ТОЛЬКО getUpdates:
class _DropGetUpdates(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "getUpdates" not in record.getMessage()
        except Exception:
            return True

logging.getLogger("httpx").addFilter(_DropGetUpdates())

def build_application() -> Application:
    settings = load_settings()

    # ВАЖНО: передаём только api_key, чтобы не ловить конфликт сигнатуры.
    openai = OpenAIHelper(
        api_key=settings.openai_api_key
        # Если ваш OpenAIHelper поддерживает другие параметры, можно добавить позже,
        # но сейчас сохраняем совместимость с вашей актуальной версией.
    )

    # ⚠️ Создаём bot заранее, чтобы передать его setup_commands в post_init:
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    # ✅ post_init вызовется автоматически в процессе initialize():
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(bot.setup_commands)  # <-- ключевой момент
    )
    app = builder.build()

    # Регистрируем все хендлеры
    bot.install(app)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # Синхронный блокирующий вызов (PTB сам управляет event loop):
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])


if __name__ == "__main__":
    main()
