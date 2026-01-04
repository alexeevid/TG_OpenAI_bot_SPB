# app/handlers/text.py
import logging
from typing import Dict, List

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.authz_service import AuthzService
from ..services.rag_service import RagService
from ..core.types import RetrievedChunk

log = logging.getLogger(__name__)


def _format_kb_context(results: List[RetrievedChunk]) -> str:
    lines = []
    for r in results:
        src = r.meta.get("source", "")
        page = r.meta.get("page", "")
        score = r.score
        chunk = (r.text or "").strip()
        if len(chunk) > 800:
            chunk = chunk[:800] + "…"
        lines.append(f"- Источник: {src} стр.{page} (score={score:.3f})\n  {chunk}")
    return "\n".join(lines)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    gs: GenService = context.bot_data.get("svc_gen")
    rag: RagService = context.bot_data.get("svc_rag")
    cfg = context.bot_data.get("settings")

    if not ds or not gs or not cfg or not update.effective_user:
        await update.effective_message.reply_text("⚠️ Сервисы не настроены.")
        return

    d = ds.ensure_active_dialog(update.effective_user.id)
    settings = ds.get_active_settings(update.effective_user.id)

    model = settings.get("text_model") or cfg.text_model
    mode = settings.get("mode") or "detailed"

    # system prompt
    sys = (
        "Ты — профессиональный ассистент. "
        "Отвечай на русском, структурировано и предметно. "
        "Если пользователь просит ссылки/цитаты — приводи их. "
    )
    if mode == "short":
        sys += "Пиши максимально кратко, только по сути."
    elif mode == "detailed":
        sys += "Пиши развёрнуто, с чек-листами и шагами."

    # RAG from KB
    results: List[RetrievedChunk] = []
    if rag:
        try:
            results = rag.search_in_active_dialog(d.id, text, k=6)
        except Exception as e:
            log.exception("RAG search failed: %s", e)
            results = []

    if results:
        kb_ctx = _format_kb_context(results)
        sys = (
            sys
            + "\n\nЕсли в данных из базы знаний есть прямые ответы — опирайся на них. "
              "Не выдумывай источники.\n\n"
              "Данные из базы знаний:\n"
            + kb_ctx
        )

    history_rows = ds.history(d.id, limit=24)
    history: List[Dict[str, str]] = [
        {"role": m.role, "content": m.content}
        for m in history_rows if m
    ]

    try:
        answer = await gs.generate_text(
            model=model,
            system=sys,
            history=history,
            user_text=text,
        )
    except Exception as e:
        log.exception("GEN failed: %s", e)
        await update.effective_message.reply_text("⚠️ Ошибка генерации.")
        return

    ds.add_message(d.id, role="user", content=text)
    ds.add_message(d.id, role="assistant", content=answer)

    await update.effective_message.reply_text(answer)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not getattr(update, "message", None):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await process_text(update, context, text)


def register(app: Application) -> None:
    # Catch-all для текста должен идти с низким приоритетом, чтобы не “съедать” состояния ConversationHandler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=10)
