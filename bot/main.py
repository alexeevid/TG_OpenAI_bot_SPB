import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from bot.config import load_settings
from bot.telegram_bot import ChatGPTTelegramBot
from bot.db.session import init_db
from bot.db.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("🚀 Запуск Telegram-бота")

    # Загружаем настройки из окружения
    settings = load_settings()

    # Инициализация БД
    logger.info("🔄 Инициализация базы данных...")
    init_db()

    # Создаём бота
    bot = ChatGPTTelegramBot(settings)

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("dialogs", bot.cmd_dialogs))
    app.add_handler(CommandHandler("rename", bot.cmd_rename))
    app.add_handler(CommandHandler("export", bot.cmd_export))
    app.add_handler(CommandHandler("kb_diag", bot.cmd_kb_diag))
    app.add_handler(CommandHandler("fix_db", bot.cmd_fix_db))
    
    # Обработка текстовых сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    # Обработка callback-кнопок
    app.add_handler(CallbackQueryHandler(bot.on_callback))

    logger.info("🤖 Бот запущен в режиме polling")
    app.run_polling()

if __name__ == "__main__":
    main()
