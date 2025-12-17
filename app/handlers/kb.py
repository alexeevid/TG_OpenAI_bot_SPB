from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Функциональность базы знаний в разработке.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_handler))
