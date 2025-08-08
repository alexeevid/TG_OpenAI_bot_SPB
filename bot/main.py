import logging

from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

from bot.telegram_bot import ChatGPTTelegramBot, on_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    bot = ChatGPTTelegramBot()
    app = ApplicationBuilder().token(bot.settings.telegram_bot_token).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("reset", bot.cmd_reset))
    app.add_handler(CommandHandler("stats", bot.cmd_stats))
    app.add_handler(CommandHandler("kb", bot.cmd_kb))

    # CallbackQueryHandler для работы с БЗ
    app.add_handler(CallbackQueryHandler(bot.on_kb_callback, pattern=r"^kb:"))

    # Глобальный обработчик ошибок
    app.add_error_handler(on_error)

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
