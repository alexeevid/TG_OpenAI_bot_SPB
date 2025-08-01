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

    # Создаем OpenAI helper (не меняем сигнатуры вашего класса)
    openai = OpenAIHelper(api_key=settings.openai_api_key)

    # Создаем объект бота (наш класс с обработчиками)
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    # post_init — выполняется один раз перед началом polling
    async def _post_init(app: Application):
        # 1) На всякий случай убираем webhook и сбрасываем «зависшие» апдейты
        await app.bot.delete_webhook(drop_pending_updates=True)
        # 2) Логируем, под каким ботом мы подключены
        me = await app.bot.get_me()
        logger.info("🤖 Connected as @%s (id=%s)", me.username, me.id)
        # 3) Выставляем команды (ваш метод)
        try:
            await bot.setup_commands(app)
        except Exception as e:
            logger.exception("setup_commands failed: %s", e)

    # Сборка Application с post_init
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
    )
    app = builder.build()

    # Регистрируем хендлеры
    bot.install(app)
    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")

    # Важные параметры:
    # - drop_pending_updates=True — выкидываем «старые» апдейты,
    # - allowed_updates=None — разрешаем все типы апдейтов (по умолчанию PTB сам выберет нужные)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,
    )


if __name__ == "__main__":
    main()
