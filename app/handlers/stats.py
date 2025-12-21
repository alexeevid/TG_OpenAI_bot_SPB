from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService
# DialogService will be accessed via bot_data, no direct import needed

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return
    d = ds.get_active_dialog(update.effective_user.id)
    history = ds.history(d.id, limit=1000)
    total = len(history)
    user_count = sum(1 for m in history if getattr(m, "role", "") == "user")
    assistant_count = sum(1 for m in history if getattr(m, "role", "") == "assistant")
    stats_text = (f"Сообщений в диалоге: {total}\n"
                  f"Сообщений пользователя: {user_count}\n"
                  f"Сообщений ассистента: {assistant_count}")
    await update.message.reply_text(stats_text)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", stats_handler))
