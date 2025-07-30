# bot/main.py
from __future__ import annotations

import logging
from telegram.ext import Application

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper


def setup_logging() -> None:
    """
    Базовая настройка логов + приглушение болтливых библиотек.
    Дополнительно фильтруем строки с /getUpdates.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )

    # приглушаем шумные либы
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.request").setLevel(logging.WARNING)
    logging.getLogger("yadisk").setLevel(logging.WARNING)

    # Если хотите скрыть ТОЛЬКО строки с getUpdates — оставьте фильтр;
    # если хотите видеть все HTTP-запросы, удалите блок ниже.
    class _DropGetUpdates(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                return "getUpdates" not in record.getMessage()
            except Exception:
                return True

    logging.getLogger("httpx").addFilter(_DropGetUpdates())


# Настраиваем логирование и создаём logger ДО первого использования
setup_logging()
logger = logging.getLogger(__name__)


def build_application() -> Application:
    settings = load_settings()

    # Создаём OpenAI helper.
    # ВАЖНО: передаём только api_key — так совместимо с вашей текущей реализацией OpenAIHelper.
    openai = OpenAIHelper(api_key=settings.openai_api_key)

    # Создаём экземпляр бота (нужен заранее, чтобы передать setup_commands в post_init)
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    # Строим приложение и регистрируем post_init: он выставит команды и меню
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(bot.setup_commands)  # метод должен существовать в telegram_bot.py (async def setup_commands(app): ...)
    )
    app = builder.build()

    # Регистрируем все обработчики
    bot.install(app)

    return app


def main() -> None:
    logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    app = build_application()

    logger.info("🚀 Бот запускается (run_polling)...")
    # Синхронный блокирующий вызов — PTB сам управляет event loop.
    app.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"]
    )


if __name__ == "__main__":
    main()
