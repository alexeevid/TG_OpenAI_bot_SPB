
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.authz_service import AuthzService

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data['svc_authz']
    uid = update.effective_user.id
    role = "admin" if az.is_admin(uid) else "user"
    await update.message.reply_text(f"Вы: {uid}, роль: {role}")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Контекст диалога сброшен (stub).")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("reset", cmd_reset))
