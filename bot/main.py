# bot/main.py
from __future__ import annotations

import logging
import os
import sys

# Advisory-lock для защиты от второго процесса
try:
    import fcntl  # недоступен на Windows, но Railway на Linux
except Exception:  # pragma: no cover
    fcntl = None

from telegram.ext import Application

from bot.config import load_settings
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db
from bot.db.models import Base


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)


def _acquire_advisory_lock() -> None:
    """
    Простой advisory-lock на файловой системе, чтобы гарантировать, что
    не запустится второй экземпляр бота (иначе 409 Conflict от getUpdates).
    """
    if fcntl is None:
        logger.warning("fcntl недоступен — пропускаю advisory-lock (OK для локального запуска)")
        return
    lock_path = "/tmp/tg_openai_bot.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    except OSError:
        logger.error("🚫 Уже запущен другой экземпляр бота (lock %s). Завершаюсь.", lock_path)
        sys.exit(1)


def build_application() -> Application:
    """
    Инициализация настроек, БД, OpenAI-хелпера и Telegram-приложения.
    """
    settings = load_settings()

    # Инициализация БД (обязательно перед запуском приложения)
    init_db(Base)

    # OpenAI helper. ВАЖНО: используем 'default_model' и 'temperature'.
    openai = OpenAIHelper(
        api_key=settings.openai_api_key,
        default_model=getattr(settings, "openai_model", None),
        image_model=getattr(settings, "image_model", None),
        temperature=float(getattr(settings, "openai_temperature", 0.2)),
        enable_image_generation=bool(getattr(settings, "enable_image_generation", True)),
        settings=settings,  # чтобы работали whitelist/denylist и др.
    )

    # Telegram app
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Устанавливаем все handlers и сервисы
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)
    bot.install(app)

    return app


def main() -> None:
    _acquire_advisory_lock()

    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # run_polling — синхронный метод-обёртка, сам управляет asyncio-циклом
    app.run_polling(
        allowed_updates=None,  # можно ограничить типы апдейтов при необходимости
        stop_signals=None,     # используем дефолтную обработку сигналов
        poll_interval=1.0,
        timeout=10,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
