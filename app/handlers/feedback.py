from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /feedback <сообщение>")
        return
    feedback_text = " ".join(context.args)
    admin_chat_id = None
    if context.bot_data.get("settings"):
        admin_chat_id = context.bot_data["settings"].admin_chat_id
    if admin_chat_id:
        try:
            await context.bot.send_message(chat_id=admin_chat_id,
                                           text=f"Feedback от пользователя {update.effective_user.id}:\n{feedback_text}")
            await update.message.reply_text("✅ Спасибо, ваш отзыв отправлен.")
        except Exception as e:
            await update.message.reply_text("⚠️ Не удалось отправить отзыв.")
    else:
        await update.message.reply_text("✅ Спасибо, ваш отзыв принят.")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("feedback", feedback_handler))
