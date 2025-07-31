# bot/main.py
from __future__ import annotations

import logging
import sys
from typing import Optional

from telegram.ext import Application
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, Conflict

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper

# --- Логи --------------------------------------------------------------
# Умерим болтливость httpx/PTB: оставим WARNING и ошибки.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)


def _build_request(settings) -> HTTPXRequest:
    """
    Создаём HTTPXRequest с явными таймаутами.
    Это уменьшит вероятность длинных зависаний на shutdown и ReadTimeout в логах.
    """
    # Вы можете отрегулировать значения под свою инфраструктуру.
    return HTTPXRequest(
        read_timeout=getattr(settings, "tg_read_timeout", 15),     # чтение ответа
        write_timeout=getattr(settings, "tg_write_timeout", 15),   # отправка запроса
        connect_timeout=getattr(settings, "tg_connect_timeout", 10),
        pool_timeout=getattr(settings, "tg_pool_timeout", 10),
    )


async def _on_error(update, context) -> None:
    """
    Унифицированный error handler для PTB.
    Не даём нефатальным сетевым исключениям «красить» логи и ронять поток.
    """
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logging.getLogger("bot.telegram_bot").warning("Non-fatal network issue: %s", err)
        return
    if isinstance(err, Conflict):
        # Обычно это означает, что запущена вторая копия бота
        logging.getLogger("bot.telegram_bot").warning(
            "Conflict: another getUpdates is running for this token. Stop other instance."
        )
        return

    # Остальное логируем как error со стеком
    logging.getLogger("bot.telegram_bot").exception("Unhandled exception in handler: %s", err)


def build_application() -> Application:
    settings = load_settings()

    # Инициализируем OpenAIHelper строго с теми аргументами, которые он поддерживает
    openai = OpenAIHelper(api_key=settings.openai_api_key)

    # Конфигурируем транспорт с таймаутами
    request = _build_request(settings)

    # Создаём инстанс бота и пробрасываем его post_init для установки меню команд
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(request)
        .post_init(bot.setup_commands)  # команды/меню выставляются один раз на старте
    )

    app = builder.build()

    # Регистрируем все обработчики обновлений
    bot.install(app)

    # Глобальный обработчик ошибок PTB
    app.add_error_handler(_on_error)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    try:
        # Важно: run_polling — синхронный блокирующий вызов;
        # PTB сам создаёт/закрывает event loop.
        app.run_polling(
            allowed_updates=["message", "edited_message", "callback_query"],
            # На старте удалим «хвосты» (если бот был выключен какое-то время)
            drop_pending_updates=True,
            # Немного уменьшили интервал опроса, чтобы быстрее реагировать
            poll_interval=1.0,
            # А эти таймауты PTB передаст в HTTPXRequest, если не заданы выше;
            # мы уже задали их в _build_request, так что можно не повторять:
            # read_timeout=15, write_timeout=15, connect_timeout=10, pool_timeout=10,
        )
    except Conflict:
        # Самый частый сценарий: уже есть другой процесс бота
        logger.error(
            "Another instance is polling getUpdates. "
            "Убедитесь, что запущена только одна копия бота (Railway Replicas=1, локальный процесс остановлен)."
        )
    except TimedOut as e:
        # Сетевой таймаут в процессе остановки/рестарта — не фатален
        logger.warning("Telegram network timeout on shutdown: %s", e)
    except Exception as e:
        logger.exception("Unexpected fatal error: %s", e)


if __name__ == "__main__":
    main()
