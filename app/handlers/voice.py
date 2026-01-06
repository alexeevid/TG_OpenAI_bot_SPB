# app/handlers/voice.py
from __future__ import annotations

import logging
from typing import Any, Dict

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.dialog_service import DialogService

log = logging.getLogger(__name__)

DRAW_PREFIXES = ("нарисуй", "рисуй", "draw")


def _extract_draw_prompt(text: str) -> str | None:
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    for p in DRAW_PREFIXES:
        if low.startswith(p):
            rest = t[len(p) :].strip()
            return rest or None
    return None


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    cfg = context.application.bot_data.get("settings")
    vs = context.application.bot_data.get("svc_voice")
    if not vs:
        await msg.reply_text("⚠️ VoiceService не инициализирован.")
        return

    ds: DialogService | None = context.application.bot_data.get("svc_dialog")
    dialog_settings: Dict[str, Any] = {}
    transcribe_model: str | None = None
    image_model: str | None = None

    if ds:
        try:
            _ = ds.ensure_active_dialog(update.effective_user.id)
            dialog_settings = ds.get_active_settings(update.effective_user.id) or {}
            transcribe_model = dialog_settings.get("transcribe_model")
            image_model = dialog_settings.get("image_model")
        except Exception as e:
            log.warning("Failed to read dialog settings for voice models: %s", e)

    await msg.reply_text("🎙️ Распознаю…")

    try:
        # ТВОЯ текущая сигнатура: transcribe(message)
        # Но если VoiceService уже поддерживает model/dialog_settings — используем.
        try:
            text = await vs.transcribe(msg, model=transcribe_model, dialog_settings=dialog_settings)
        except TypeError:
            try:
                text = await vs.transcribe(msg, model=transcribe_model)
            except TypeError:
                text = await vs.transcribe(msg)
    except Exception as e:
        log.exception("Voice transcription failed: %s", e)
        await msg.reply_text(f"❌ Ошибка распознавания голоса: {e}")
        return

    if not text:
        await msg.reply_text("⚠️ Не удалось распознать речь.")
        return

    # Если распознанный текст начинается с "нарисуй ..." — генерируем картинку
    prompt = _extract_draw_prompt(text)
    if prompt:
        if not getattr(cfg, "enable_image_generation", False):
            await msg.reply_text("🚫 Генерация изображений отключена в настройках.")
            return

        img_svc = context.application.bot_data.get("svc_image")
        if img_svc is None:
            await msg.reply_text("⚠️ Сервис генерации изображений не инициализирован.")
            return

        await msg.reply_text(f"🎨 Понял: «{prompt}». Рисую…")
        try:
            # Аналогично image.py: не ломаем контракт сервиса
            try:
                url = await img_svc.generate_url(prompt, model=image_model, dialog_settings=dialog_settings)
            except TypeError:
                try:
                    url = await img_svc.generate_url(prompt, model=image_model)
                except TypeError:
                    url = await img_svc.generate_url(prompt)

            await msg.reply_text(url)
        except Exception as e:
            log.exception("Image generation failed (voice trigger): %s", e)
            await msg.reply_text(f"❌ Ошибка генерации изображения: {e}")
        return

    # Иначе — обычная обработка текста через общий пайплайн
    try:
        from .text import process_text
        await process_text(update, context, text)
    except Exception as e:
        log.exception("process_text failed after voice: %s", e)
        await msg.reply_text(f"❌ Ошибка обработки: {e}")


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
