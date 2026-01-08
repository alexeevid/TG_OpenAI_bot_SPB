# app/handlers/web.py
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.search_service import SearchService
from ..services.authz_service import AuthzService


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    az: AuthzService | None = context.bot_data.get("svc_authz")
    if az and not az.is_allowed(update.effective_user.id):
        await msg.reply_text("⛔ Доступ запрещен.")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await msg.reply_text("Использование: /web <запрос>\nНапример: /web разработка устава проекта PMI")
        return

    svc: SearchService | None = context.bot_data.get("svc_search")
    if not svc:
        await msg.reply_text("⚠️ Веб-поиск не настроен.")
        return

    res = svc.search(query, max_results=7)
    if not res:
        await msg.reply_text("Нет результатов (или веб-поиск выключен).")
        return

    await msg.reply_text("🔎 Результаты веб-поиска:\n\n" + "\n\n".join(res))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("web", cmd_web))
