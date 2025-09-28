
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Карточка активного диалога (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", cmd_stats))
