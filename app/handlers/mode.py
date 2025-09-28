
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Стиль ответа (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("mode", cmd_mode))
