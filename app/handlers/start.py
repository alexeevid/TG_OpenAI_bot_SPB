from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.dialog_service import DialogService


WELCOME = (
    "Готово. Я на связи.\n\n"
    "Команды:\n"
    "/dialogs — список диалогов\n"
    "/reset — новый диалог\n"
    "/model — выбрать модель (для текущего диалога)\n"
    "/mode — режим ответа (concise|detailed|mcwilliams)\n"
    "/img <описание> — изображение (если включено)\n"
    "/help — справка\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ds: DialogService = context.bot_data.get("svc_dialog")
    if ds and update.effective_user:
        ds.get_active_dialog(update.effective_user.id)
    await update.message.reply_text(WELCOME)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
