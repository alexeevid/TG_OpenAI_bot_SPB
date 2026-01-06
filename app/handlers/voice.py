# app/handlers/voice.py
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

log = logging.getLogger(__name__)

DRAW_PREFIXES = ("нарисуй", "рисуй", "draw")


def _extract_draw_prompt(text: str) -> str | None:
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    for p in DRAW_PREFIXES:
        if low.startswith(p):
            rest = t[len(p):].strip()
            return rest or None
    return None


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.application.bot_data.get("settings")
    vs = context.application.bot_data.get("svc_voice")
    if not vs:
        await update.effective_message.reply_text("⚠️ VoiceService не инициализирован.")
        return

    # Получаем файл
    voice = update.effective_message.voice or update.effective_message.audio
    if not voice:
        return

    await update.effective_message.reply_text("🎙️ Распознаю…")

    try:
        tg_file = await voice.get_file()
        # VoiceService в твоём проекте обычно умеет работать с telegram.File
        text = await vs.transcribe_telegram_file(tg_file)  # если у тебя метод называется иначе — скажи, поправлю
    except AttributeError:
        # fallback: если VoiceService принимает Update/Context иначе
        text = await vs.transcribe(update, context)
    except Exception as e:
        log.exception("Voice transcription failed: %s", e)
        await update.effective_message.reply_text(f"❌ Ошибка распознавания голоса: {e}")
        return

    if not text:
        await update.effective_message.reply_text("⚠️ Не удалось распознать речь.")
        return

    # Если голос начинается с "нарисуй ..." — запускаем генерацию изображения
    prompt = _extract_draw_prompt(text)
    if prompt:
        if not getattr(cfg, "enable_image_generation", False):
            await update.effective_message.reply_text("🚫 Генерация изображений отключена в настройках.")
            return
        img_svc = context.application.bot_data.get("svc_image")
        if img_svc is None:
            await update.effective_message.reply_text("⚠️ Сервис генерации изображений не инициализирован.")
            return

        await update.effective_message.reply_text(f"🎨 Понял: «{prompt}». Рисую…")
        try:
            url = await img_svc.generate_url(prompt)
            await update.effective_message.reply_text(url)
        except Exception as e:
            log.exception("Image generation failed (voice trigger): %s", e)
            await update.effective_message.reply_text(f"❌ Ошибка генерации изображения: {e}")
        return

    # Иначе — обычная обработка текста через общий пайплайн
    try:
        from .text import process_text
        await process_text(update, context, text)
    except Exception as e:
        log.exception("process_text failed after voice: %s", e)
        await update.effective_message.reply_text(f"❌ Ошибка обработки: {e}")


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
