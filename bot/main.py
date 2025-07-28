import logging
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from sqlalchemy import text

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db, engine  # <- берем engine для advisory-lock
from bot.db.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOCK_KEY = 751234567890123456  # любой фиксированный bigint < 9.22e18

def ensure_singleton_or_exit():
    """Гарантируем запуск только одного инстанса через pg_try_advisory_lock."""
    try:
        with engine.begin() as conn:
            got = conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})
            if not got:
                logger.error("🛑 Найден другой запущенный инстанс (advisory-lock). Завершение.")
                raise SystemExit(0)
        logger.info("🔒 Advisory-lock получен. Запускаем бота.")
    except Exception as e:
        logger.error(f"Ошибка при попытке захватить advisory-lock: {e}")
        # На всякий случай завершаем, чтобы не плодить дубли
        raise SystemExit(1)

def build_application():
    settings = load_settings()
    # Инициализация схемы БД (создаст таблицы при первом запуске)
    init_db(Base)

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # OpenAI + Бот
    openai = OpenAIHelper(api_key=settings.openai_api_key, default_model=settings.openai_model)
    bot = ChatGPTTelegramBot(openai=openai, settings=settings)

    # Если у бота есть install(app) — используем её
    try:
        bot.install(app)  # внутри должны регистрироваться все handlers
    except AttributeError:
        # Фолбэк: базовый обработчик текста
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    return app

def main():
    ensure_singleton_or_exit()
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
