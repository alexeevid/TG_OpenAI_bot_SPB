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


async def on_draw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = context.application.bot_data.get("settings")
    if not getattr(cfg, "enable_image_generation", False):
        await update.effective_message.reply_text("🚫 Генерация изображений отключена в настройках.")
        return

    img_svc = context.application.bot_data.get("svc_image")
    if img_svc is None:
        await update.effective_message.reply_text("⚠️ Сервис генерации изображений не инициализирован.")
        return

    text = (update.effective_message.text or "").strip()
    prompt = _extract_draw_prompt(text)

    # поддержка /draw <prompt>
    if not prompt:
        parts = text.split(maxsplit=1)
        if parts and parts[0].lstrip("/").lower() in ("draw", "image", "img"):
            prompt = parts[1].strip() if len(parts) > 1 else None

    if not prompt:
        await update.effective_message.reply_text("Напиши: «нарисуй <что рисовать>» или /draw <описание>.")
        return

    await update.effective_message.reply_text("🎨 Рисую…")

    try:
        # Можно брать размер/модель из настроек, если у тебя они есть
        url = await img_svc.generate_url(prompt)
        await update.effective_message.reply_text(url)
    except Exception as e:
        log.exception("Image generation failed: %s", e)
        await update.effective_message.reply_text(f"❌ Ошибка генерации изображения: {e}")


def register(app: Application) -> None:
    # /draw <prompt>
    app.add_handler(CommandHandler("draw", on_draw))

    # текстовые триггеры "нарисуй ..."
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_draw),
        group=50,  # поздняя группа, чтобы не мешать обычному тексту
    )
