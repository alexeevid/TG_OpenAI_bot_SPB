# app/handlers/image.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ApplicationHandlerStop

from ..services.dialog_service import DialogService

log = logging.getLogger(__name__)

DRAW_PREFIXES = ("нарисуй", "рисуй", "draw")


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


def _extract_draw_prompt(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    low = t.lower()
    for p in DRAW_PREFIXES:
        if low.startswith(p):
            return t[len(p):].strip(" :,-\n\t")
    return None


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

    # --- dialog settings ---
    dialog_settings: Dict[str, Any] = {}
    if ds:
        try:
            dialog_settings = ds.get_active_settings(update.effective_user.id) or {}
        except Exception:
            dialog_settings = {}

    image_model = (
        dialog_settings.get("image_model")
        or getattr(cfg, "image_model", None)
        or getattr(cfg, "openai_image_model", None)
        or "gpt-image-1"
    )

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
        except Exception:
            pass

    await msg.reply_text("🎨 Рисую…")

    try:
        # Совместимость по контрактам svc_image.generate_url(...)
        try:
            url = await img_svc.generate_url(prompt, model=image_model, dialog_settings=dialog_settings)
        except TypeError:
            try:
                url = await img_svc.generate_url(prompt, model=image_model)
            except TypeError:
                url = await img_svc.generate_url(prompt)

        # Скрываем URL: отправляем как фото
        try:
            await msg.reply_photo(photo=url, caption="Готово.")
        except Exception:
            # если Telegram не принял URL как фото — тогда уже показываем ссылку
            await msg.reply_text(str(url))

        # --- MULTIMODAL CONTEXT: сохраняем шаг в историю и в context_assets ---
        if ds:
            try:
                ds.add_message(
                    tg_user_id=update.effective_user.id,
                    role="assistant",
                    text=f"[image]{url}",
                )
            except Exception:
                pass

            try:
                assets = context.chat_data.get("context_assets") or []
                assets.append({"type": "image_url", "url": url})
                context.chat_data["context_assets"] = assets
            except Exception:
                pass

    except Exception as e:
        log.exception("Image generation failed: %s", e)
        await msg.reply_text(f"❌ Ошибка генерации изображения: {e}")


async def on_draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /img <prompt> или /draw <prompt>
    msg = update.effective_message
    if not msg:
        return

    text = (msg.text or "").strip()
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else None
    if not prompt:
        await msg.reply_text("Напиши: /img <что рисовать> (или /draw …, или текстом: «нарисуй …»).")
        return

    await _generate_and_reply(update, context, prompt)


async def on_draw_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Триггер: "нарисуй ..." / "рисуй ..." / "draw ..."
    msg = update.effective_message
    if not msg:
        return

    prompt = _extract_draw_prompt(msg.text or "")
    if not prompt:
        return

    await _generate_and_reply(update, context, prompt)

    # Останавливаем дальнейшие обработчики (иначе text.py тоже ответит на это сообщение)
    raise ApplicationHandlerStop


def register(app: Application) -> None:
    # Основная команда: /img. Алиас: /draw (для совместимости)
    app.add_handler(CommandHandler("img", on_draw_command))
    app.add_handler(CommandHandler("draw", on_draw_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_draw_text_trigger))
