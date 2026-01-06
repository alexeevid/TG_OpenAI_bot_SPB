# app/handlers/image.py
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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


async def _generate_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    cfg = context.application.bot_data.get("settings")
    if not getattr(cfg, "enable_image_generation", False):
        await update.effective_message.reply_text("🚫 Генерация изображений отключена в настройках.")
        return

    img_svc = context.application.bot_data.get("svc_image")
    if img_svc is None:
        await update.effective_message.reply_text("⚠️ Сервис генерации изображений не инициализирован.")
        return

    await update.effective_message.reply_text("🎨 Рисую…")

    try:
        url = await img_svc.generate_url(prompt)
        await update.effective_message.reply_text(url)
    except Exception as e:
        log.exception("Image generation failed: %s", e)
        await update.effective_message.reply_text(f"❌ Ошибка генерации изображения: {e}")


async def on_draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /draw <prompt>
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else None
    if not prompt:
        await update.effective_message.reply_text("Напиши: /draw <что рисовать> (или текстом: «нарисуй …»).")
        return
    await _generate_and_reply(update, context, prompt)


async def on_draw_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Триггер только если текст начинается с "нарисуй/рисуй/draw"
    text = (update.effective_message.text or "").strip()
    prompt = _extract_draw_prompt(text)
    if not prompt:
        return
    await _generate_and_reply(update, context, prompt)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("draw", on_draw_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_draw_text_trigger))
