from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Пока статистика диалога не реализована. Скоро будет!")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", stats_handler))
