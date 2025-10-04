
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from ..services.gen_service import GenService
from ..services.dialog_service import DialogService

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ds: DialogService = context.bot_data['svc_dialog']
    gen: GenService = context.bot_data['svc_gen']
    d = ds.get_or_create_active(update.effective_user.id)
    question = update.message.text
    ds.add_user_message(d.id, question)
    ans = await gen.chat(user_msg=question, dialog_id=d.id)
    ds.add_assistant_message(d.id, ans.text)
    await update.message.reply_text(ans.text)

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
