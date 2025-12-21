from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

async def update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    syncer = context.bot_data.get("svc_syncer")
    if not syncer:
        await update.message.reply_text("⚠️ Синхронизация базы знаний недоступна.")
        return
    result = syncer.sync()
    if result.get("status") == "ok":
        count = result.get("indexed", 0)
        await update.message.reply_text(f"✅ База знаний обновлена. Новых фрагментов: {count}.")
    else:
        error_msg = result.get("message", "неизвестная ошибка")
        await update.message.reply_text(f"⚠️ Ошибка обновления базы знаний: {error_msg}")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("update", update_handler))
