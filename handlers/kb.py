from __future__ import annotations

import logging
from math import ceil

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.dialog_kb_service import DialogKBService
from ..services.rag_service import RagService
from ..db.repo_kb import KBRepo
from .kb_ui import kb_select_show

log = logging.getLogger(__name__)

HELP = """База знаний (встроена в диалоги)

Основное:
/kb                     — справка
/kb select              — выбрать документы для текущего диалога (интерфейс)
/kb list                — список документов, подключённых к диалогу
/kb catalog [стр]       — каталог документов БЗ (глобально)
/kb stats               — статистика БЗ (глобально)
/kb stats dialog         — статистика БЗ для текущего диалога
/kb <запрос>             — поиск по БЗ с учётом выбранных документов диалога
"""


async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    az: AuthzService = context.bot_data.get("svc_authz")
    if az and not az.is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    dkb: DialogKBService = context.bot_data.get("svc_dialog_kb")
    kb_repo: KBRepo = context.bot_data.get("repo_kb")
    rag: RagService = context.bot_data.get("svc_rag")

    if not ds or not dkb or not kb_repo or not rag:
        await update.effective_message.reply_text("⚠️ Сервисы БЗ не настроены.")
        return

    d = ds.get_active_dialog(update.effective_user.id)
    if not d:
        await update.effective_message.reply_text("⚠️ Активный диалог не найден. Используйте /dialogs.")
        return

    args = context.args or []
    sub = (args[0].lower().strip() if args else "")

    if not sub:
        await update.effective_message.reply_text(HELP)
        return

    if sub == "select":
        await kb_select_show(update, context, page=1)
        return

    if sub == "list":
        try:
            items = dkb.list_attached(d.id)
            if not items:
                await update.effective_message.reply_text("Нет подключённых документов. Используйте /kb select.")
                return
            lines = ["Документы диалога:"]
            for it in items:
                did = int(it["document_id"])
                st = "✅" if it.get("is_enabled") else "➖"
                title = (it.get("title") or "").strip()
                path = (it.get("path") or "").strip()
                label = title if title else path
                lines.append(f"{st} id={did} — {label}")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb list failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка списка KB.")
        return

    if sub == "catalog":
        try:
            page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
            items, total = kb_repo.catalog(page=page, page_size=10, search="")
            pages = max(1, ceil(total / 10))

            attached = dkb.list_attached(d.id)
            attached_map = {int(x["document_id"]): bool(x["is_enabled"]) for x in attached}

            def badge(doc_id: int) -> str:
                if doc_id not in attached_map:
                    return "⬜"
                return "✅" if attached_map[doc_id] else "➖"

            if not items:
                await update.effective_message.reply_text("Каталог пуст. Сначала выполните /update (админ), чтобы индексировать БЗ.")
                return

            lines = [f"Каталог БЗ: страница {page}/{pages}, всего документов: {total}"]
            for it in items:
                did = int(it["id"])
                chunks = int(it.get("chunks") or 0)
                title = (it.get("title") or "").strip()
                path = (it.get("path") or "").strip()
                label = title if title else path
                lines.append(f"{badge(did)} id={did} · chunks={chunks} · {label}")

            lines.append("\nДля выбора документов используйте: /kb select")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb catalog failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка каталога KB.")
        return

    if sub == "stats":
        try:
            scope = (args[1].lower().strip() if len(args) > 1 else "")
            if scope == "dialog":
                st = dkb.stats_dialog(d.id)
                await update.effective_message.reply_text(
                    "KB stats (диалог)\n"
                    f"- dialog_id: {d.id}\n"
                    f"- attached: {st['attached']}\n"
                    f"- enabled: {st['enabled']}\n"
                    f"- documents_in_scope: {st['documents_in_scope']}\n"
                    f"- chunks_in_scope: {st['chunks_in_scope']}"
                )
            else:
                st = kb_repo.stats_global()
                lines = [
                    "KB stats (глобально)",
                    f"- documents: {st['documents']}",
                    f"- chunks: {st['chunks']}",
                    "",
                    "Top documents by chunks:",
                ]
                for r in st["top_docs"]:
                    title = (r.get("title") or "").strip()
                    path = (r.get("path") or "").strip()
                    label = title if title else path
                    lines.append(f"- id={r['id']} · chunks={r['chunks']} · {label}")
                await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb stats failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка статистики KB.")
        return

    # default: treat args as query
    query = " ".join(args).strip()
    if not query:
        await update.effective_message.reply_text(HELP)
        return

    try:
        results = rag.retrieve(query, dialog_id=d.id, top_k=5)
    except Exception as e:
        log.exception("kb search failed: %s", e)
        await update.effective_message.reply_text("⚠️ Ошибка поиска KB.")
        return

    if not results:
        await update.effective_message.reply_text("Ничего не найдено. Проверьте, что документы выбраны в /kb select.")
        return

    lines = [f"Найдено фрагментов: {len(results)}"]
    for i, chunk in enumerate(results, start=1):
        text = (chunk.text or "").strip()
        if len(text) > 400:
            text = text[:400] + "..."
        src = (chunk.document_title or chunk.document_path or "").strip()
        if src:
            lines.append(f"{i}. {text}\n   источник: {src}")
        else:
            lines.append(f"{i}. {text}")

    await update.effective_message.reply_text("\n".join(lines))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_handler))
