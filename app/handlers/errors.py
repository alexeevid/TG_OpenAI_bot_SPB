from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("UNHANDLED ERROR: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Подробности записаны в лог."
            )
    except Exception:
        pass


def register(app: Application) -> None:
    app.add_error_handler(on_error)
