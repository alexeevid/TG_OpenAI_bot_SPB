
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

HELP = (
    "/start — приветствие\n"
    "/help — полный список команд\n"
    "/dialogs — список диалогов\n"
    "/dialog_new — создать новый диалог\n"
    "/kb — подключить/отключить документы из БЗ\n"
    "/stats — карточка активного диалога\n"
    "/model — выбрать модель\n"
    "/mode — стиль ответа (pro/expert/user/ceo)\n"
    "/img <описание> — генерация изображения\n"
    "/web <запрос> — веб-поиск\n"
    "/reset — сброс контекста активного диалога\n"
    "/whoami — мои права"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я готов к работе. Наберите /help.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
