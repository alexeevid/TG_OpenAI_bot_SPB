
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data['svc_authz']
    uid = update.effective_user.id
    role = "admin" if az.is_admin(uid) else "user"
    await update.message.reply_text(f"Вы: {uid}, роль: {role}")

from ..services.dialog_service import DialogService

async def cmd_reset(update, context):
    ds: DialogService = context.bot_data['svc_dialog']
    d = ds.new_dialog(update.effective_user.id)
    await update.message.reply_text(f"Создан новый активный диалог #{d.id}")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("reset", cmd_reset))
