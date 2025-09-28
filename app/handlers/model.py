
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбор модели (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("model", cmd_model))
