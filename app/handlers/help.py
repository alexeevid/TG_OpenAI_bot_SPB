from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — начать работу\n"
    "/help — справка по командам\n"
    "/reset — начать новый диалог\n"
    "/dialogs — управление диалогами (выбор / удаление / переименование)\n"
    "/status — информация о текущем диалоге\n"
    "/model — выбрать модель\n"
    "/mode — выбрать стиль ответа\n"
    "/img <описание> — сгенерировать изображение\n"
    "/kb <запрос> — поиск по базе знаний\n"
    "/config — текущая конфигурация\n"
    "/about — о проекте\n"
    "/feedback <текст> — оставить отзыв\n"
)

ADMIN_TEXT = (
    "\nАдмин-команды:\n"
    "/access — управление доступом (allow/block/admin/list)\n"
    "/update — синхронизировать базу знаний\n"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService | None = context.application.bot_data.get("svc_authz") or context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("Доступ запрещен.")
        return

    text = HELP_TEXT
    if az and update.effective_user and az.is_admin(update.effective_user.id):
        text += ADMIN_TEXT

    await update.effective_message.reply_text(text)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
