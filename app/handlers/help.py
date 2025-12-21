from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

HELP_TEXT = (
    "📌 *Доступные команды:*\n"
    "/start — начать работу\n"
    "/help — справка по командам\n"
    "/reset — начать новый диалог\n"
    "/dialogs — список текстом\n"
    "/dialog <id> — переключить диалог\n"
    "/menu — меню управления диалогами\n"
    "/status — информация о текущем диалоге\n"
    "/model — выбрать модель\n"
    "/mode — выбрать стиль ответа\n"
    "/img <описание> — сгенерировать изображение\n"
    "/kb <запрос> — поиск по базе знаний\n"
    "/update — синхронизировать базу знаний\n"
    "/config — текущая конфигурация\n"
    "/about — о проекте\n"
    "/feedback <текст> — оставить отзыв"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
