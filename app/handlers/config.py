from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    cfg = context.application.bot_data.get("settings")
    if cfg:
        reply = f"Текущая модель: {cfg.text_model}\nИзображения: {'Да' if cfg.enable_image_generation else 'Нет'}"
    else:
        reply = "Настройки не найдены."
    await update.message.reply_text(reply)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("config", config_handler))
