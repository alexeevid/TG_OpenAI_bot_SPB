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


def _system_prompt(mode: str | None) -> str:
    base = "Ты ассистент в Telegram-боте. Отвечай по-русски, структурировано и по делу."
    if mode == "concise":
        return base + " Отвечай кратко, максимум 6-10 строк, без лишних вступлений."
    if mode == "mcwilliams":
        return base + " Используй стиль Мак-Вильямс: наблюдения, гипотезы, осторожные формулировки, без категоричности."
    return base + " Давай примеры и рекомендации, если уместно."


def _format_kb_context(chunks: List[RetrievedChunk]) -> str:
    """
    Best practice: краткие выдержки + источники.
    Сильно не раздуваем prompt.
    """
    lines = []
    for i, c in enumerate(chunks, start=1):
        txt = (c.text or "").strip().replace("\n", " ")
        if len(txt) > 380:
            txt = txt[:380] + "..."
        src = ""
        if c.document_title or c.document_path:
            label = (c.document_title or c.document_path or "").strip()
            src = f" (источник: {label})"
        lines.append(f"{i}. {txt}{src}")
    return "\n".join(lines)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ds: DialogService = context.bot_data.get("svc_dialog")
    gen: GenService = context.bot_data.get("svc_gen")
    if not ds or not gen or not update.effective_user:
        await update.message.reply_text("⚠️ Сервисы не настроены.")
        return

    d = ds.get_active_dialog(update.effective_user.id)
    settings = ds.get_active_settings(update.effective_user.id)
    model = settings.get("text_model") or getattr(context.bot_data.get("settings"), "text_model", None)
    mode = settings.get("mode") or "detailed"

    sys = _system_prompt(mode)

    # ---- FAIL-SAFE KB ----
    rag: RagService = context.bot_data.get("svc_rag")
    results: List[RetrievedChunk] = []
    if rag:
        try:
            results = rag.retrieve(text, dialog_id=d.id, top_k=4)
        except Exception as e:
            # критично: KB не должна валить основной ответ
            log.exception("RAG retrieve failed (ignored): %s", e)
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
        for m in history_rows if m.role in ("user", "assistant")
    ]

    ds.add_user_message(d.id, text)

    try:
        answer = await gen.chat(text, history=history, model=model, system_prompt=sys)
    except Exception as e:
        log.exception("GEN failed: %s", e)
        await update.message.reply_text("⚠️ Ошибка генерации... Попробуйте /model и выберите другую модель.")
        return

    ds.add_assistant_message(d.id, answer)

    # Опционально: если KB использовалась — добавить короткий блок источников
    if results:
        src_lines = []
        seen = set()
        for c in results:
            key = (c.document_id, c.document_title, c.document_path)
            if key in seen:
                continue
            seen.add(key)
            label = (c.document_title or c.document_path or "").strip()
            if label:
                src_lines.append(f"- {label}")
        if src_lines:
            answer = answer + "\n\nИсточники (БЗ):\n" + "\n".join(src_lines[:6])

    await update.message.reply_text(answer)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        az: AuthzService = context.bot_data.get("svc_authz")
        if az and not az.is_allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещен.")
            return

    if not update.message or not update.message.text:
        return

    # Обработка переименования диалога (сохраняем вашу текущую логику)
    if "rename_dialog_id" in context.user_data:
        new_name = update.message.text.strip()
        dialog_id = context.user_data.pop("rename_dialog_id")
        repo = context.bot_data.get("repo_dialogs")
        if repo:
            repo.rename_dialog(dialog_id, new_name)
            await update.message.reply_text(f"✅ Диалог переименован в: {new_name}")
        return

    await process_text(update, context, update.message.text.strip())


def register(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
