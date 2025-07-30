# bot/main.py
from __future__ import annotations

import logging
from telegram.ext import Application

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

async def _post_init(app: Application):
    """
    Гарантированно выключаем webhook при старте,
    чтобы точно работать в режиме polling без конфликта.
    """
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook удалён (drop_pending_updates=True). Работаем в polling.")
    except Exception as e:
        logger.warning("Не удалось удалить webhook: %s", e)

def build_application() -> Application:
    settings = load_settings()

    openai = OpenAIHelper(api_key=settings.openai_api_key)

    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)         # <-- важная строка
        .post_init(bot.setup_commands) # <-- ваши команды в глобальных scope
    )
    app = builder.build()

    # Регистрируем обработчики
    bot.install(app)

    # (опционально) единый обработчик ошибок, чтобы не видеть "No error handlers are registered"
    async def _on_error(update, context):
        logger.exception("Unhandled exception: %s", context.error)
    app.add_error_handler(_on_error)

    return app

def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # drop_pending_updates=True — лишние апдейты в очереди Телеграма удаляются при старте
    app.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
