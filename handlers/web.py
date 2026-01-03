
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ..services.search_service import SearchService

async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args) if context.args else ""
    svc: SearchService = context.bot_data['svc_search']
    res = svc.search(query)
    await update.message.reply_text("\n".join(res) if res else "Нет результатов.")

def register(app: Application) -> None:
    app.add_handler(CommandHandler("web", cmd_web))
