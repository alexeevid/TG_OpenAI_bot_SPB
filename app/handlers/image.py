# app/handlers/image.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ApplicationHandlerStop

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


async def _generate_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    cfg = context.application.bot_data.get("settings")
    if not getattr(cfg, "enable_image_generation", False):
        await msg.reply_text("🚫 Генерация изображений отключена в настройках.")
        return

    img_svc = context.application.bot_data.get("svc_image")
    if img_svc is None:
        await msg.reply_text("⚠️ Сервис генерации изображений не инициализирован.")
        return

    ds: DialogService | None = context.application.bot_data.get("svc_dialog")
    dialog_settings: Dict[str, Any] = {}
    image_model: Optional[str] = None

    if ds:
        try:
            _ = ds.ensure_active_dialog(update.effective_user.id)
            dialog_settings = ds.get_active_settings(update.effective_user.id) or {}
            image_model = dialog_settings.get("image_model")
        except Exception as e:
            log.warning("Failed to read dialog settings for image model: %s", e)

    # --- Normalize & sync model (so /status matches actual used model) ---
    openai = _get_openai_client(context)
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

    await msg.reply_text("🎨 Рисую…")

    try:
        # Не ломаем контракт сервиса: пробуем расширенный вызов, иначе — старый.
        # 1) пробуем (prompt, model=..., dialog_settings=...)
        try:
            url = await img_svc.generate_url(prompt, model=image_model, dialog_settings=dialog_settings)
        except TypeError:
            # 2) пробуем (prompt, model=...)
            try:
                url = await img_svc.generate_url(prompt, model=image_model)
            except TypeError:
                # 3) старый контракт (prompt)
                url = await img_svc.generate_url(prompt)

        await msg.reply_text(url)

        # --- MULTIMODAL CONTEXT: сохраняем шаг в историю и в context_assets ---
        if ds:
            try:
                d = ds.ensure_active_dialog(update.effective_user.id)
            except Exception:
                d = None

            # 1) История (чтобы диалог не "обнулялся")
            try:
                if d:
                    ds.add_user_message(d.id, f"НАРИСУЙ: {prompt}")
            except Exception:
                pass
            try:
                if d:
                    ds.add_assistant_message(d.id, url or "")
            except Exception:
                pass

            # 2) Asset (единый механизм через DialogService)
            try:
                ds.add_dialog_asset(
                    update.effective_user.id,
                    {
                        "type": "generated_image",
                        "kind": "openai",
                        "caption": prompt,
                        "url": url,
                        "model": image_model,
                    },
                    keep_last=5,
                )
            except Exception:
                pass

    except Exception as e:
        log.exception("Image generation failed: %s", e)
        await msg.reply_text(f"❌ Ошибка генерации изображения: {e}")


async def on_draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /draw <prompt>
    msg = update.effective_message
    if not msg:
        return

    text = (msg.text or "").strip()
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else None
    if not prompt:
        await msg.reply_text("Напиши: /draw <что рисовать> (или текстом: «нарисуй …»).")
        return
    await _generate_and_reply(update, context, prompt)


async def on_draw_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Триггер только если текст начинается с "нарисуй/рисуй/draw"
    msg = update.effective_message
    if not msg:
        return

    text = (msg.text or "").strip()
    prompt = _extract_draw_prompt(text)
    if not prompt:
        return

    await _generate_and_reply(update, context, prompt)
    # ВАЖНО: останавливаем дальнейшие обработчики (иначе text.py тоже ответит на это сообщение)
    raise ApplicationHandlerStop


def register(app: Application) -> None:
    app.add_handler(CommandHandler("draw", on_draw_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_draw_text_trigger))
