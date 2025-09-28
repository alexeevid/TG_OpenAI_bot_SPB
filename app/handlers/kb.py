
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def cmd_kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Подключение/отключение документов БЗ (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", cmd_kb))
