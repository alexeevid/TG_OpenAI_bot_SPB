import logging

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from bot.settings import settings
from bot.telegram_bot import ChatGPTTelegramBot, on_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    # Инициализируем бота с настройками
    bot = ChatGPTTelegramBot(settings)

    # Создаём приложение Telegram
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # --- Команды ---
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("dialogs", bot.cmd_dialogs))
    app.add_handler(CommandHandler("rename", bot.cmd_rename))
    app.add_handler(CommandHandler("export", bot.cmd_export))
    app.add_handler(CommandHandler("reset", bot.cmd_reset))
    app.add_handler(CommandHandler("model", bot.cmd_model))
    app.add_handler(CommandHandler("mode", bot.cmd_mode))
    app.add_handler(CommandHandler("img", bot.cmd_img))
    app.add_handler(CommandHandler("kb", bot.cmd_kb))
    app.add_handler(CommandHandler("kb_diag", bot.cmd_kb_diag))
    app.add_handler(CommandHandler("stats", bot.cmd_stats))

    # --- CallbackQuery ---
    # Сначала перехватываем всё, что начинается с "kb:", чтобы кнопки БЗ не уехали в общий обработчик
    app.add_handler(CallbackQueryHandler(bot.on_callback, pattern=r"^kb:"))
    # Затем общий обработчик всех остальных колбэков (dlg:*, model:*, mode:* и т.д.)
    app.add_handler(CallbackQueryHandler(bot.on_callback))

    # --- Текстовые сообщения ---
    # Последним — чтобы команды и колбэки обработались раньше
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    # --- Глобальный обработчик ошибок ---
    app.add_error_handler(on_error)

    logger.info("Бот запущен…")
    app.run_polling()


if __name__ == "__main__":
    main()
