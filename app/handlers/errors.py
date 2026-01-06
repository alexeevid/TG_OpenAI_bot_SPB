# app/handlers/errors.py
from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error

    # Polling conflict: during rolling deploy старый процесс может не успеть умереть.
    # Самое надёжное — завершить процесс, Railway поднимет чистый инстанс.
    if isinstance(err, Conflict):
        log.warning("Telegram polling conflict detected (another getUpdates). Exiting to release lock.")
        os._exit(1)

    # transient network errors: без шумных traceback
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Telegram network error: %s", err)
        return

    log.exception("UNHANDLED ERROR: %s", err)

    # Best-effort user message
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Подробности записаны в лог."
            )
    except Exception:
        pass


def register(app: Application) -> None:
    app.add_error_handler(on_error)
