from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from telegram.ext import ApplicationBuilder

from bot.config import load_settings
from bot.db.session import init_db
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

LOCK_FILE = "/tmp/tg_bot.lock"

@asynccontextmanager
async def advisory_lock(path: str):
    """
    Простейший advisory-lock на уровне файловой системы, чтобы на Railway не запустились
    два poller'а одновременно (иначе будут конфликты getUpdates 409/Conflict).
    """
    if os.path.exists(path):
        logger.info("🔒 Advisory-lock уже существует. Второй процесс завершен.")
        raise SystemExit(0)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

def build_application():
    settings = load_settings()

    # 1) Инициализация БД (создаем таблицы, если их ещё нет)
    init_db()

    # 2) OpenAI helper
    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        model=getattr(settings, "openai_model", None),
        image_model=getattr(settings, "image_model", None),
        temperature=getattr(settings, "openai_temperature", 0.2),
        enable_image_generation=bool(getattr(settings, "enable_image_generation", True)),
    )

    # 3) Telegram bot (handlers + колбэк post_init)
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    # 4) PTB Application + правильная регистрация post_init ЧЕРЕЗ BUILDER!
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(bot._post_init)  # ВАЖНО: post_init задаётся на BUILDER, а не вызывается у Application!
        .concurrent_updates(True)
        .build()
    )

    # 5) Регистрируем все хэндлеры
    bot.install(app)

    return app

def main():
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    async def _run():
        async with advisory_lock(LOCK_FILE):
            app = build_application()
            logger.info("🚀 Бот запускается (run_polling)...")
            await app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])
    asyncio.run(_run())

if __name__ == "__main__":
    main()
