from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

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
    "/stats — статистика текущего диалога\n"
    "/kb <запрос> — поиск по базе знаний\n"
    "/update — обновить базу знаний\n"
    "/config — текущая конфигурация\n"
    "/about — о проекте\n"
    "/feedback — оставить отзыв\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return
    await update.message.reply_text(HELP_TEXT)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("help", cmd_help))
