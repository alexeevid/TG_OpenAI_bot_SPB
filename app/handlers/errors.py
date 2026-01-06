from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error

    # 1) Polling conflict: this happens when two instances call getUpdates concurrently
    #    (common during Railway rolling deploy). Best action: exit fast so the platform restarts cleanly.
    if isinstance(err, Conflict):
        log.warning("Telegram polling conflict detected (another getUpdates). Exiting to release lock.")
        os._exit(1)

    # 2) Transient network errors: log as warning without noisy traceback
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Telegram network error: %s", err)
        return

    # 3) Everything else: full traceback
    log.exception("UNHANDLED ERROR: %s", err)

    # Optional user-facing message (best-effort)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Подробности записаны в лог."
            )
    except Exception:
        pass


def register(app: Application) -> None:
    app.add_error_handler(on_error)
