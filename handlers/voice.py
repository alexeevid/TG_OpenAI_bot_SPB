import logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from ..services.voice_service import VoiceService
from .text import process_text
from ..services.authz_service import AuthzService

log = logging.getLogger(__name__)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        az: AuthzService = context.bot_data.get("svc_authz")
        if az and not az.is_allowed(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Доступ запрещен.")
            return
    if not update.message or not update.effective_message:
        return
    vs: VoiceService = context.bot_data.get("svc_voice")
    if not vs:
        await update.effective_message.reply_text("⚠️ Распознавание голоса не настроено.")
        return
    try:
        text = await vs.transcribe(update.message)
    except Exception as e:
        log.exception("VOICE transcribe failed: %s", e)
        await update.effective_message.reply_text("⚠️ Ошибка распознавания.")
        return
    if not text or text.startswith("[ошибка"):
        await update.effective_message.reply_text(text or "⚠️ Не удалось распознать речь.")
        return
    await update.effective_message.reply_text(f"🗣️ {text}")
    # Process transcribed text as a regular message
    await process_text(update, context, text)

def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
