from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)

_conflict_exit_scheduled = False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _conflict_exit_scheduled
    err = context.error

    # Polling conflict (two instances calling getUpdates).
    # Do NOT exit immediately: it creates a restart storm during rolling deploy.
    # Instead: wait a bit, then exit once.
    if isinstance(err, Conflict):
        if not _conflict_exit_scheduled:
            _conflict_exit_scheduled = True
            log.warning(
                "Telegram polling conflict detected (another getUpdates). "
                "Will exit in 25s to release lock and avoid restart storm."
            )
            await asyncio.sleep(25)
            os._exit(1)
        return

    # transient network errors: without noisy traceback
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
