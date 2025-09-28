
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.dialog_service import DialogService

async def cmd_dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ds: DialogService = context.bot_data['svc_dialog']
    d = ds.ensure_dialog(update.effective_user.id)
    await update.message.reply_text(f"Создан диалог #{d.id}")

async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Список диалогов (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialog_new", cmd_dialog_new))
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))
