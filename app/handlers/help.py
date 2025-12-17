from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

HELP_TEXT = (
    "Команды:\n"
    "/start — приветствие\n"
    "/help — справка\n"
    "/reset — новый диалог\n"
    "/dialogs — список ваших диалогов\n"
    "/dialog <id> — переключить текущий диалог\n"
    "/model — выбрать модель для текущего диалога\n"
    "/mode — режим ответа: concise | detailed | mcwilliams\n"
    "/img <описание> — сгенерировать изображение\n"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
