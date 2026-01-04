# app/main.py
from __future__ import annotations

import logging

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import get_settings
from .handlers import (
    admin,
    dialogs,
    help as help_handler,
    image,
    kb,
    model,
    mode,
    reset,
    stats,
    status,
    text,
    voice,
)

log = logging.getLogger(__name__)


def _configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_application() -> Application:
    """
    Собираем PTB Application и регистрируем хендлеры.

    Важно: схема БД НЕ создаётся здесь. Схема управляется Alembic-миграциями
    (в Railway/Docker: python -m alembic upgrade head перед запуском бота).
    """
    settings = get_settings()
    _configure_logging(settings.LOG_LEVEL)

    app = Application.builder().token(settings.TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", help_handler.start_handler))
    app.add_handler(CommandHandler("help", help_handler.help_handler))
    app.add_handler(CommandHandler("reset", reset.reset_handler))
    app.add_handler(CommandHandler("stats", stats.stats_handler))
    app.add_handler(CommandHandler("kb", kb.kb_handler))
    app.add_handler(CommandHandler("model", model.model_handler))
    app.add_handler(CommandHandler("dialogs", dialogs.dialogs_handler))
    app.add_handler(CommandHandler("dialog", dialogs.dialog_open_handler))
    app.add_handler(CommandHandler("status", status.status_handler))

    # Admin / KB sync
    app.add_handler(CommandHandler("kb_sync", admin.kb_sync_handler))
    app.add_handler(CommandHandler("kb_reindex", admin.kb_reindex_handler))

    # Callback queries
    app.add_handler(CallbackQueryHandler(model.model_callback_handler, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(mode.mode_callback_handler, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(dialogs.dialogs_callback_handler, pattern=r"^dlg:"))
    app.add_handler(CallbackQueryHandler(kb.kb_callback_handler, pattern=r"^kb:"))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE, voice.voice_handler))
    app.add_handler(MessageHandler(filters.PHOTO, image.photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text.text_handler))

    return app


def run() -> None:
    """
    Точка входа: run_polling (long-running процесс).

    Важно: в Railway нельзя запускать два инстанса polling одновременно,
    иначе будет 409 Conflict (getUpdates).
    """
    app = build_application()
    app.run_polling(allowed_updates=Application.ALL_TYPES)


if __name__ == "__main__":
    run()
