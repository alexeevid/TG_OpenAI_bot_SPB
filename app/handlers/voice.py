
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from ..services.voice_service import VoiceService
from ..services.gen_service import GenService
from ..services.dialog_service import DialogService

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("VOICE: получен голос длиной %s", update.message.voice.duration)
    file = await update.message.voice.get_file()
    b = await file.download_as_bytearray()
    voice: VoiceService = context.bot_data['svc_voice']
    ds: DialogService = context.bot_data['svc_dialog']
    gen: GenService = context.bot_data['svc_gen']
    d = ds.ensure_dialog(update.effective_user.id)
    tr = await voice.transcribe(bytes(b), lang_hint=None)
    ds.add_user_message(d.id, tr.text)
    ans = await gen.chat(user_msg=tr.text, dialog_id=d.id)
    ds.add_assistant_message(d.id, ans.text)
    await update.message.reply_text(ans.text)

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
