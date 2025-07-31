# bot/main.py
from __future__ import annotations

import fcntl
import os
import sys
import logging
from telegram.ext import Application

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

# ----- Логирование -----
logger = logging.getLogger(__name__)
# Единая настройка логгера: INFO по умолчанию
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

LOCK_FILE = None  # дескриптор файла для file-lock


def _acquire_singleton_lock(token: str) -> None:
    """
    Простой файловый замок, чтобы предотвратить второй параллельный запуск
    в том же окружении/контейнере. Для Railway это полезно при повторном запуске
    процесса с тем же volume.
    """
    global LOCK_FILE
    lock_path = f"/tmp/tg-bot-{token}.lock"
    LOCK_FILE = open(lock_path, "w")
    try:
        fcntl.flock(LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        LOCK_FILE.write(str(os.getpid()))
        LOCK_FILE.flush()
    except BlockingIOError:
        print(
            "Another bot process is already running (file lock). Exiting.",
            file=sys.stderr,
        )
        sys.exit(1)


def build_application() -> Application:
    settings = load_settings()

    # Файловый замок — до инициализации всего остального
    _acquire_singleton_lock(settings.telegram_bot_token)

    # Важно: передаём только api_key — без параметров, которых может не быть в вашей версии OpenAIHelper
    openai = OpenAIHelper(
        api_key=settings.openai_api_key
    )

    # Создаём бота заранее — чтобы передать post_init для регистрации меню/команд И очистки вебхука
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(bot.setup_commands_and_cleanup)  # пост-инициализация: delete_webhook + команды
    )

    # Можно настроить таймауты, чтобы избежать зависаний при плохой сети:
    if hasattr(builder, "get_updates_http_version"):
        # опционально: builder.get_updates_http_version("1.1")
        pass

    app = builder.build()

    # Регистрация всех хендлеров
    bot.install(app)

    # Error handler — чтобы не было "No error handlers are registered"
    app.add_error_handler(bot.on_error)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # drop_pending_updates=True — чтобы очистить старые висящие апдейты и уменьшить шанс конфликтов при рестартах
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )


if __name__ == "__main__":
    main()
