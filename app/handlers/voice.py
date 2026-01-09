# app/handlers/voice.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.dialog_service import DialogService
from ..core.utils import with_mode_prefix

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


def _get_openai_client(context: ContextTypes.DEFAULT_TYPE):
    # main.py кладёт alias "openai" и "oai_client"
    return context.application.bot_data.get("openai") or context.application.bot_data.get("oai_client")


def _safe_model(openai, *, model: Optional[str], kind: str, fallback: str) -> str:
    """
    Soft normalize model to an available one. Best effort; never raises.
    """
    if not openai:
        return model or fallback
    try:
        return openai.ensure_model_available(model=model, kind=kind, fallback=fallback)
    except Exception:
        return model or fallback


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    cfg = context.application.bot_data.get("settings")
    vs = context.application.bot_data.get("svc_voice")
    if not vs:
        await msg.reply_text(with_mode_prefix(context, update.effective_user.id, "⚠️ VoiceService не инициализирован."))
        return

    ds: DialogService | None = context.application.bot_data.get("svc_dialog")
    dialog_settings: Dict[str, Any] = {}
    transcribe_model: Optional[str] = None
    image_model: Optional[str] = None

    if ds:
        try:
            _ = ds.ensure_active_dialog(update.effective_user.id)
            dialog_settings = ds.get_active_settings(update.effective_user.id) or {}
            transcribe_model = dialog_settings.get("transcribe_model")
            image_model = dialog_settings.get("image_model")
        except Exception as e:
            log.warning("Failed to read dialog settings for voice models: %s", e)

    # --- Normalize models against real availability BEFORE calling services ---
    openai = _get_openai_client(context)

    safe_transcribe = _safe_model(
        openai,
        model=transcribe_model,
        kind="transcribe",
        fallback=getattr(cfg, "transcribe_model", None) or getattr(cfg, "openai_transcribe_model", None) or "whisper-1",
    )
    if ds and safe_transcribe and safe_transcribe != transcribe_model:
        try:
            ds.update_active_settings(update.effective_user.id, {"transcribe_model": safe_transcribe})
            dialog_settings["transcribe_model"] = safe_transcribe
            transcribe_model = safe_transcribe
        except Exception as e:
            log.warning("Failed to sync transcribe_model to dialog settings: %s", e)

    safe_image = _safe_model(
        openai,
        model=image_model,
        kind="image",
        fallback=getattr(cfg, "image_model", None) or getattr(cfg, "openai_image_model", None) or "gpt-image-1",
    )
    if ds and safe_image and safe_image != image_model:
        try:
            ds.update_active_settings(update.effective_user.id, {"image_model": safe_image})
            dialog_settings["image_model"] = safe_image
            image_model = safe_image
        except Exception as e:
            log.warning("Failed to sync image_model to dialog settings: %s", e)

    await msg.reply_text(with_mode_prefix(context, update.effective_user.id, "🎙️ Распознаю…"))

    try:
        # VoiceService уже поддерживает model/dialog_settings — используем.
        try:
            text = await vs.transcribe(msg, model=transcribe_model, dialog_settings=dialog_settings)
        except TypeError:
            try:
                text = await vs.transcribe(msg, model=transcribe_model)
            except TypeError:
                text = await vs.transcribe(msg)
    except Exception as e:
        log.exception("Voice transcription failed: %s", e)
        await msg.reply_text(with_mode_prefix(context, update.effective_user.id, f"❌ Ошибка распознавания голоса: {e}"))
        return

    if not text:
        await msg.reply_text(with_mode_prefix(context, update.effective_user.id, "⚠️ Не удалось распознать речь."))
        return

    # Если распознанный текст начинается с "нарисуй ..." — генерируем картинку
    prompt = _extract_draw_prompt(text)
    if prompt:
        if not getattr(cfg, "enable_image_generation", False):
            await msg.reply_text(with_mode_prefix(context, update.effective_user.id, "🚫 Генерация изображений отключена в настройках."))
            return

        img_svc = context.application.bot_data.get("svc_image")
        if img_svc is None:
            await msg.reply_text(with_mode_prefix(context, update.effective_user.id, "⚠️ Сервис генерации изображений не инициализирован."))
            return

        await msg.reply_text(with_mode_prefix(context, update.effective_user.id, f"🎨 Понял: «{prompt}». Рисую…"))
        try:
            # Аналогично image.py: не ломаем контракт сервиса
            try:
                url = await img_svc.generate_url(prompt, model=image_model, dialog_settings=dialog_settings)
            except TypeError:
                try:
                    url = await img_svc.generate_url(prompt, model=image_model)
                except TypeError:
                    url = await img_svc.generate_url(prompt)

            await msg.reply_text(with_mode_prefix(context, update.effective_user.id, url))
        except Exception as e:
            log.exception("Image generation failed (voice trigger): %s", e)
            await msg.reply_text(with_mode_prefix(context, update.effective_user.id, f"❌ Ошибка генерации изображения: {e}"))
        return

    # Иначе — обычная обработка текста через общий пайплайн
    try:
        from .text import process_text

        await process_text(update, context, text)
    except Exception as e:
        log.exception("process_text failed after voice: %s", e)
        await msg.reply_text(with_mode_prefix(context, update.effective_user.id, f"❌ Ошибка обработки: {e}"))


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
