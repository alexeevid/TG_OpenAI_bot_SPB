from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService

WELCOME = (
    "👋 *Готово. Я на связи.*\n\n"
    "📍 Команды:\n"
    "/reset — начать новый диалог\n"
    "/dialogs — управление диалогами\n"
    "/status — информация о текущем диалоге\n"
    "/model — выбрать модель\n"
    "/mode — выбрать стиль ответа\n"
    "/kb <запрос> — поиск по базе знаний\n"
    "/img <описание> — сгенерировать изображение\n"
    "\nПодробнее: /help"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    ds: DialogService = context.bot_data.get("svc_dialog")
    if ds and update.effective_user:
        ds.get_active_dialog(update.effective_user.id)
    await update.message.reply_text(WELCOME, parse_mode="Markdown")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
