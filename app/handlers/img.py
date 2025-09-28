
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.image_service import ImageService

async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else "(пусто)"
    svc: ImageService = context.bot_data['svc_image']
    url = svc.generate(prompt)
    await update.message.reply_text(f"Изображение: {url}")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("img", cmd_img))
