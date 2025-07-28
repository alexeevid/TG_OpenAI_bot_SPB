import logging
from telegram.ext import ApplicationBuilder, MessageHandler, filters

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.db.session import init_db
from bot.db.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        # Фолбэк: хотя бы базовый обработчик текста
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    # (Необязательно) Пример установки команд через post_init — только если хотите меню сразу:
    # async def _post_init(app_):
    #     from telegram import BotCommand
    #     await app_.bot.set_my_commands([
    #         BotCommand("start", "Запуск и меню"),
    #         BotCommand("help", "Помощь"),
    #         BotCommand("reset", "Сброс контекста"),
    #         BotCommand("stats", "Статистика"),
    #         BotCommand("kb", "База знаний"),
    #         BotCommand("model", "Выбор модели"),
    #         BotCommand("dialogs", "Список диалогов / возврат"),
    #     ])
    # app.post_init(_post_init)

    return app

def main():
    app = build_application()
    logger.info("🚀 Бот запускается (run_polling)...")
    # Синхронный запуск — без asyncio.run и без await
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
