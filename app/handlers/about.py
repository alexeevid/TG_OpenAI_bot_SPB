from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

ABOUT_TEXT = "TG OpenAI БОТ v3: поддерживает голосовые сообщения, генерацию изображений и поиск по базе знаний."

async def about_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(ABOUT_TEXT)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("about", about_handler))
