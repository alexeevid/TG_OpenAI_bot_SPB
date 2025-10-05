from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

HELP_TEXT = (
    "👋 Команды:\n"
    "/start — приветствие и инициализация\n"
    "/help — эта справка\n"
    "/reset — новый диалог\n"
    "/dialogs — список ваших диалогов\n"
    "/dialog <id> — переключить текущий диалог\n"
    "/model <имя> — выбрать модель для текущего диалога (напр. gpt-4o-mini)\n"
    "/mode <режим> — стиль ответа: concise | detailed | mcwilliams\n"
    "/img <описание> — сгенерировать изображение\n"
    "/stats — статистика бота\n"
    "/kb — работа с базой знаний\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
