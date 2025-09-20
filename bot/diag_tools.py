
from typing import List, Tuple
from telegram import Update
from telegram.ext import ContextTypes, Application
from datetime import datetime

async def dump_handlers(app: Application) -> List[Tuple[int, List[str]]]:
    res = []
    for grp, handlers in (app.handlers or {}).items():
        names = []
        for h in handlers:
            cb = getattr(h, "callback", None)
            names.append(getattr(cb, "__name__", repr(h)))
        res.append((grp, names))
    return res

async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    info = await context.bot.get_webhook_info()
    lines = [
        f"Diag @ {datetime.utcnow().isoformat()}Z",
        f"webhook url: {info.url or '—'}",
        f"pending_update_count: {getattr(info, 'pending_update_count', '—')}",
        f"allowed_updates: {','.join(info.allowed_updates or []) or 'ALL'}",
        "",
        "Handlers:",
    ]
    for grp, names in await dump_handlers(app):
        lines.append(f"  group {grp}: {', '.join(names)}")
    await update.effective_message.reply_text("\n".join(lines[:4000]))
