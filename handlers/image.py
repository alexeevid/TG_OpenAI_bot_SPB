import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.image_service import ImageService
from ..services.authz_service import AuthzService

log = logging.getLogger(__name__)

async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    try:
        if not context.args:
            await update.message.reply_text("Использование: /img <описание>")
            return
        prompt = " ".join(context.args).strip()
        isvc: ImageService = context.bot_data.get("svc_image")
        if not isvc:
            await update.message.reply_text("⚠️ Генерация изображений не настроена.")
            return
        url = isvc.generate(prompt)
        await update.message.reply_text(url)
    except Exception as e:
        log.exception("/img failed: %s", e)
        await update.message.reply_text("⚠️ Ошибка генерации изображения.")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("img", cmd_img))
