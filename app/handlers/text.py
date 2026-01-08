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
from ..core.response_modes import build_system_prompt

log = logging.getLogger(__name__)


def _format_kb_context(results: List[RetrievedChunk]) -> str:
    parts: List[str] = []
    for r in results:
        title = r.get("title") or r.get("source") or "Документ"
        path = r.get("path") or ""
        score = r.get("score")
        text = r.get("text") or ""
        hdr = f"- [{title}] {path}"
        if score is not None:
            try:
                hdr += f" (score={float(score):.3f})"
            except Exception:
                pass
        parts.append(hdr + "\n" + text.strip())
    return "\n\n".join(parts)


def _system_prompt(mode: str) -> str:
    # Сохраняем внешний контракт файла (используется ниже),
    # но всю логику режимов держим в одном месте: app/core/response_modes.py
    return build_system_prompt(mode)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return

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
    settings: Dict[str, Any] = ds.get_active_settings(update.effective_user.id) or {}

    mode = str(settings.get("mode") or "professional")
    sys = _system_prompt(mode)

    results: List[RetrievedChunk] = []
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

    kb_ctx = ""
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

    try:
        used_model = meta.get("used_model")
        if used_model and isinstance(used_model, str):
            current_model = settings.get("text_model")
            if current_model != used_model:
                ds.update_active_settings(update.effective_user.id, {"text_model": used_model})
                settings["text_model"] = used_model
    except Exception as e:
        log.warning("Failed to sync used text model to dialog settings: %s", e)

    try:
        ds.add_message(d.id, role="user", content=text)
    except Exception:
        pass

    try:
        ds.add_message(d.id, role="assistant", content=answer or "")
    except Exception:
        pass

    if kb_debug and results:
        try:
            await msg.reply_text(f"🔎 KB chunks: {len(results)} (top_k={kb_top_k}, min_score={kb_min_score})")
        except Exception:
            pass

    await msg.reply_text(answer or "⚠️ Пустой ответ.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not update.message:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # подавление ответа на ввод имени при переименовании
    try:
        suppress_id = context.user_data.get("suppress_text_message_id")
        if suppress_id and int(suppress_id) == int(update.message.message_id):
            context.user_data.pop("suppress_text_message_id", None)
            return
    except Exception:
        pass

    await process_text(update, context, text)


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=10)
