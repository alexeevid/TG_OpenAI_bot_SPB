from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService

START_TEXT = (
    "Канал: https://t.me/alexeev_id\n\n"
    "Команды:\n"
    "/reset — начать новый диалог\n"
    "/dialogs — управление диалогами\n"
    "/status — информация о текущем диалоге\n"
    "/model — выбрать модель\n"
    "/mode — выбрать стиль ответа\n"
    "/kb <запрос> — поиск по базе знаний\n"
    "/img <описание> — сгенерировать изображение\n"
    "\n"
    "Подробнее: /help"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService | None = context.application.bot_data.get("svc_authz") or context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("Доступ запрещен.")
        return

    ds: DialogService | None = context.application.bot_data.get("svc_dialog") or context.bot_data.get("svc_dialog")
    if ds and update.effective_user:
        # гарантируем наличие активного диалога (без шума в чате)
        try:
            ds.get_active_dialog(update.effective_user.id)
        except Exception:
            pass

    await update.effective_message.reply_text(START_TEXT)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
