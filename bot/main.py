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

    openai = OpenAIHelper(api_key=settings.openai_api_key)

    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    async def _post_init(app: Application):
        # 1) гарантированно отключаем вебхук и вычищаем висящие апдейты
        await app.bot.delete_webhook(drop_pending_updates=True)
        me = await app.bot.get_me()
        logger.info("🤖 Connected as @%s (id=%s)", me.username, me.id)

        # Устанавливаем команды (существующий метод бота)
        try:
            await bot.setup_commands(app)
        except Exception as e:
            logger.exception("setup_commands failed: %s", e)

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
    )
    app = builder.build()

    bot.install(app)
    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # Сбрасываем возможные старые апдейты и разрешаем все типы
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=None,  # все типы
    )


if __name__ == "__main__":
    main()
