from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.voice_service import VoiceService
from .text import process_text

log = logging.getLogger(__name__)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # При желании можно показывать транскрипт:
    await update.effective_message.reply_text(f"🗣️ {text}")

    # Дальше — как обычный текст
    await process_text(update, context, text)


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
