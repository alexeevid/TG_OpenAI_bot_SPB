# app/handlers/text.py
from __future__ import annotations

import logging
from typing import Any, Dict, List

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.gen_service import GenService
from ..services.rag_service import RagService
from ..core.types import RetrievedChunk

log = logging.getLogger(__name__)


def _format_kb_context(results: List[RetrievedChunk]) -> str:
    lines: List[str] = []
    for r in results:
        title = (r.document_title or "").strip()
        path = (r.document_path or "").strip()
        src = title if title else (path if path else f"document_id={r.document_id}")
        chunk = (r.text or "").strip()
        if len(chunk) > 900:
            chunk = chunk[:900] + "…"
        lines.append(f"- Источник: {src} | chunk#{r.id} | sim={r.score:.3f}\n  {chunk}")
    return "\n".join(lines)


def _format_kb_sources_for_user(results: List[RetrievedChunk], *, max_items: int = 5) -> str:
    """Формирует блок источников для пользователя (детерминированно).

    Зачем:
    - LLM может использовать RAG-контекст, но не написать явные ссылки.
    - Этот блок добавляется кодом, если retrieval вернул результаты выше порога.
    """
    if not results:
        return ""

    lines: List[str] = ["\n\n📚 Источники (БЗ):"]
    for i, r in enumerate(results[: max(1, int(max_items))], start=1):
        title = (r.document_title or "").strip()
        path = (r.document_path or "").strip()
        src = title if title else (path if path else f"document_id={r.document_id}")

        quote = (r.text or "").strip().replace("\n", " ")
        if len(quote) > 280:
            quote = quote[:280] + "…"

        score = f"{float(r.score):.3f}" if r.score is not None else "-"
        lines.append(f"{i}) {src} | chunk#{r.id} | sim={score}\n   «{quote}»")

    return "\n".join(lines)


def _system_prompt(mode: str) -> str:
    base = (
        "Ты — профессиональный ассистент по управлению проектами и цифровым продуктам. "
        "Отвечай на русском языке. "
        "Пиши структурировано и предметно. "
        "Не выдумывай факты и источники. "
        "Если данных недостаточно — задай уточняющие вопросы."
    )
    if mode == "brief":
        return base + " Режим: кратко (до 6–10 предложений)."
    if mode == "exec":
        return base + " Режим: для руководителя (вывод + 3–5 пунктов решений/рисков)."
    if mode == "mcwilliams":
        return (
            base
            + " Стиль: McWilliams (ясно, структурно, деловой тон, "
              "делай выводы и рекомендации, избегай воды)."
        )
    return base + " Режим: развёрнуто (пункты, примеры, рекомендации)."


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

    # AuthZ
    az: AuthzService | None = context.bot_data.get("svc_authz")
    if az and not az.is_allowed(update.effective_user.id):
        await msg.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService | None = context.bot_data.get("svc_dialog")
    gs: GenService | None = context.bot_data.get("svc_gen")
    rag: RagService | None = context.bot_data.get("svc_rag")
    cfg = context.bot_data.get("settings")

    if not ds or not gs or not cfg:
        await msg.reply_text("⚠️ Сервисы не настроены.")
        return

    d = ds.ensure_active_dialog(update.effective_user.id)

    # settings диалога: здесь живут text_model/image_model/transcribe_model и пользовательские режимы
    settings: Dict[str, Any] = ds.get_active_settings(update.effective_user.id) or {}

    # режим ответа (это именно настройка диалога)
    mode = str(settings.get("mode") or "detailed")
    sys = _system_prompt(mode)

    # --- KB / RAG retrieve (с порогом релевантности) ---
    results: List[RetrievedChunk] = []
    kb_ctx = ""
    kb_min_score = float(getattr(cfg, "kb_min_score", 0.35))
    kb_top_k = int(getattr(cfg, "max_kb_chunks", 6))
    kb_debug = bool(getattr(cfg, "kb_debug", False))

    try:
        if rag:
            results = rag.retrieve(
                query=text,
                dialog_id=d.id,
                top_k=kb_top_k,
                min_score=kb_min_score,
            )
    except Exception as e:
        log.warning("RAG retrieve failed: %s", e)
        results = []

    # Если retrieval вернул результаты — усиливаем grounded-правила
    if results:
        kb_ctx = _format_kb_context(results)
        sys = (
            sys
            + "\n\n"
            + "ВАЖНО: Ниже приведены фрагменты из базы знаний. "
              "Если вопрос можно покрыть этими фрагментами — отвечай ТОЛЬКО на их основе. "
              "Не добавляй внешние сведения. "
              "Если в фрагментах нет ответа — прямо скажи, что в базе знаний нет данных, и попроси уточнение.\n"
              "Цитаты приводи дословно и указывай источник (путь/название документа).\n\n"
              "Данные из базы знаний:\n"
            + kb_ctx
        )

    history_rows = ds.history(d.id, limit=24)
    history: List[Dict[str, str]] = [{"role": m.role, "content": m.content} for m in history_rows]

    meta: Dict[str, Any] = {}

    try:
        # model НЕ передаём: GenService сам выберет text_model из dialog_settings
        answer = await gs.chat(
            user_msg=text,
            history=history,
            model=None,
            system_prompt=sys,
            temperature=cfg.openai_temperature,
            dialog_settings=settings,
            out_meta=meta,
        )
    except Exception as e:
        log.exception("GenService.chat failed: %s", e)
        await msg.reply_text("⚠️ Ошибка генерации.")
        return

    # --- Synchronize REAL used model back into dialog settings ---
    # This fixes: "fallback used but /status shows selected (unavailable) model".
    try:
        used_model = meta.get("used_model")
        if used_model and isinstance(used_model, str):
            current_model = settings.get("text_model")
            if current_model != used_model:
                ds.update_active_settings(update.effective_user.id, {"text_model": used_model})
                settings["text_model"] = used_model
    except Exception as e:
        log.warning("Failed to sync used text model to dialog settings: %s", e)

    # --- Add deterministic KB sources block (only if retrieval passed threshold) ---
    final_answer = answer

    if results:
        # чтобы пользователь видел, что RAG реально сработал (по желанию — только в debug)
        if kb_debug:
            max_sim = max(float(r.score) for r in results if r.score is not None)
            final_answer = f"🧩 RAG: найдено {len(results)} фрагм., max_sim={max_sim:.3f}, min_score={kb_min_score:.2f}\n\n" + final_answer

        # добавляем источники, если их ещё нет
        if "Источники (БЗ)" not in final_answer:
            final_answer += _format_kb_sources_for_user(results)

    ds.add_user_message(d.id, text)
    ds.add_assistant_message(d.id, final_answer)

    await msg.reply_text(final_answer)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not getattr(update, "message", None):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # --- Suppress text processing for UI conversations (e.g., dialogs rename) ---
    # Некоторые ConversationHandler'ы используют обычное TEXT сообщение как ввод
    # (например, переименование диалога). В ряде окружений PTB такое сообщение
    # может быть обработано и ConversationHandler'ом, и нашим catch-all текстовым
    # хендлером. Чтобы бот не отвечал "как на запрос", подавляем обработку.
    try:
        suppress_id = context.user_data.get("suppress_text_message_id")
        if suppress_id and int(suppress_id) == int(update.message.message_id):
            context.user_data.pop("suppress_text_message_id", None)
            return
    except Exception:
        # если что-то пошло не так — не ломаем обычную обработку
        pass

    await process_text(update, context, text)


def register(app: Application) -> None:
    # Catch-all для текста должен идти с низким приоритетом, чтобы не “съедать” состояния ConversationHandler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=10)
