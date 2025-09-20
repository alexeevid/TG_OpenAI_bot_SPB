# Thin orchestration: assemble Application and register handlers.
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from bot.settings import settings
# Import legacy handlers to preserve behavior
from bot.legacy.telegram_legacy import (
    start, help_cmd, stats, kb, kb_cb,
    model_menu, model_cb, mode_menu, mode_cb,
    on_text, on_voice, web_cmd
)

def build_app():
    app_ = Application.builder().token(settings.bot_token).build()

    # Handlers
    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)
    app_.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice), group=0)

    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("help", help_cmd))
    app_.add_handler(CommandHandler("stats", stats))
    app_.add_handler(CommandHandler("kb", kb))
    app_.add_handler(CommandHandler("web", web_cmd))
    app_.add_handler(CommandHandler("model", model_menu))
    app_.add_handler(CommandHandler("mode", mode_menu))

    app_.add_handler(CallbackQueryHandler(kb_cb, pattern=r"^kb:"))
    app_.add_handler(CallbackQueryHandler(model_cb, pattern=r"^model:"))
    app_.add_handler(CallbackQueryHandler(mode_cb, pattern=r"^mode:"))
    return app_
