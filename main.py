# app/main.py
from __future__ import annotations

import logging
import os

import sqlalchemy
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import get_settings
from .db.session import get_engine
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


def _ensure_dialog_kb_schema(engine) -> None:
    """
    Railway-friendly bootstrap: создаём нужные таблицы без alembic/консоли.
    """
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS settings JSONB"))
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_dialog_id INTEGER"))

        # FIX: модели ожидают updated_at, но в старой схеме (001_initial) этих колонок нет
        conn.execute(sqlalchemy.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))
        conn.execute(sqlalchemy.text("ALTER TABLE dialogs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))
        conn.execute(sqlalchemy.text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"))

        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_documents (
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_documents UNIQUE(dialog_id, document_id)
            );
        """))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_dialog_id ON dialog_kb_documents (dialog_id)"
        ))
        conn.execute(sqlalchemy.text(
            "CREATE INDEX IF NOT EXISTS ix_dialog_kb_documents_document_id ON dialog_kb_documents (document_id)"
        ))

        # pdf secrets per dialog (forward compat)
        conn.execute(sqlalchemy.text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_secrets (
                dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                pdf_password TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_dialog_kb_secrets UNIQUE(dialog_id, document_id)
            );
        """))


def build_application() -> Application:
    """
    Собираем PTB Application, регистрируем хендлеры, подключаем БД.
    """
    settings = get_settings()

    # logging
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # DB engine
    engine = get_engine(settings.DATABASE_URL)
    _ensure_dialog_kb_schema(engine)

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
    Точка входа: run_polling.
    PTB ожидает coroutine. Здесь обязателен async + await.
    """
    app = build_application()

    # Важно: в Railway/Render/Heroku нельзя запускать два инстанса polling одновременно
    # иначе будет 409 Conflict (getUpdates).
    app.run_polling(allowed_updates=Application.ALL_TYPES)

if __name__ == "__main__":
    run()
