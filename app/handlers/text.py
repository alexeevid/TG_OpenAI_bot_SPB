from __future__ import annotations

import logging
from typing import Dict, List

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService

log = logging.getLogger(__name__)

# Telegram hard limit is 4096 chars per message. Keep a safety margin.
_TG_CHUNK = 3900


def _system_prompt(mode: str | None) -> str:
    base = "Ты ассистент в Telegram-боте. Отвечай по-русски, структурировано и по делу."
    if mode == "concise":
        return base + " Дай краткий ответ (до 6–10 строк), затем 3–5 буллетов с действиями."
    if mode == "detailed":
        return base + " Дай подробный ответ с разделами и примерами, но избегай воды."
    if mode == "mcwilliams":
        return base + " Пиши в стиле McWilliams: структурно, управленчески, с выводами и next steps."
    return base


def _split_text(text: str, limit: int = _TG_CHUNK) -> List[str]:
    """Split long text into Telegram-safe chunks, preferring paragraph boundaries."""
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    buf: List[str] = []
    buf_len = 0

    # split by double newlines first
    for para in text.split("\n\n"):
        p = para.strip()
        if not p:
            continue
        # if a single paragraph is too large, split hard
        if len(p) > limit:
            # flush buffer
            if buf:
                parts.append("\n\n".join(buf).strip())
                buf, buf_len = [], 0
            for i in range(0, len(p), limit):
                parts.append(p[i:i+limit])
            continue

        # try to add to buffer
        add_len = len(p) + (2 if buf else 0)
        if buf_len + add_len <= limit:
            buf.append(p)
            buf_len += add_len
        else:
            if buf:
                parts.append("\n\n".join(buf).strip())
            buf = [p]
            buf_len = len(p)

    if buf:
        parts.append("\n\n".join(buf).strip())

    # safety: ensure every chunk <= limit
    safe: List[str] = []
    for part in parts:
        if len(part) <= limit:
            safe.append(part)
        else:
            for i in range(0, len(part), limit):
                safe.append(part[i:i+limit])
    return safe


async def _send_answer(update: Update, answer: str) -> None:
    """Send answer to Telegram, safely handling long messages."""
    if not update.message:
        return
    chunks = _split_text(answer)
    try:
        # reply with first chunk to keep context
        await update.message.reply_text(chunks[0] or "…")
        for ch in chunks[1:]:
            await update.message.reply_text(ch)
    except Exception as e:
        # If sending fails, at least try to notify user with a short message.
        log.exception("Telegram send failed: %s", e)
        try:
            await update.message.reply_text("⚠️ Не удалось отправить ответ (ошибка Telegram).")
        except Exception:
            pass


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    ds: DialogService = context.bot_data.get("svc_dialog")
    gen: GenService = context.bot_data.get("svc_gen")
    if not ds or not gen or not update.effective_user:
        if update.message:
            await update.message.reply_text("⚠️ Сервисы не настроены.")
        return

    user_id = update.effective_user.id
    d = ds.get_active_dialog(user_id)

    settings = ds.get_active_settings(user_id)
    model = settings.get("text_model") or getattr(context.bot_data.get("settings"), "text_model", None)
    mode = settings.get("mode") or "mcwilliams"
    sys = _system_prompt(mode)

    # История: последние N сообщений (user/assistant)
    try:
        history_rows = ds.history(d.id, limit=24)
        history: List[Dict[str, str]] = [
            {"role": m.role, "content": m.content}
            for m in history_rows
            if m.role in ("user", "assistant") and m.content
        ]
    except Exception as e:
        log.exception("Failed to load history: %s", e)
        history = []

    # Сохраняем вопрос (но не блокируем обработку, если БД/репо временно недоступны)
    try:
        ds.add_user_message(d.id, text)
    except Exception as e:
        log.exception("Failed to persist user message: %s", e)

    try:
        answer = await gen.chat(
            text,
            history=history,
            model=model,
            system_prompt=sys,
            temperature=getattr(context.bot_data.get("settings"), "openai_temperature", None),
        )
    except Exception as e:
        log.exception("GEN failed: %s", e)
        if update.message:
            await update.message.reply_text("⚠️ Ошибка генерации ответа.")
        return

    # Persist assistant message best-effort
    try:
        ds.add_assistant_message(d.id, answer)
    except Exception as e:
        log.exception("Failed to persist assistant message: %s", e)

    await _send_answer(update, answer)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    await process_text(update, context, update.message.text)


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
