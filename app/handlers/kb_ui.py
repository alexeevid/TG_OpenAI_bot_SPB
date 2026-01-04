# app/handlers/kb_ui.py
from __future__ import annotations

from math import ceil
from typing import Dict, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from ..services.dialog_service import DialogService
from ..services.dialog_kb_service import DialogKBService
from ..db.repo_kb import KBRepo


CB_PREFIX = "kbsel"  # callback_data: kbsel:<page>:<doc_id>:<action>


def _badge(attached: Dict[int, bool], doc_id: int) -> str:
    """
    attached map: doc_id -> is_enabled
    """
    if doc_id not in attached:
        return "⬜"
    return "✅" if attached[doc_id] else "➖"


def _render_page(
    *,
    dialog_id: int,
    page: int,
    page_size: int,
    catalog_items: List[Dict],
    total: int,
    attached_map: Dict[int, bool],
) -> Tuple[str, InlineKeyboardMarkup]:
    pages = max(1, ceil(total / page_size))
    page = max(1, min(pages, int(page)))

    rows: List[List[InlineKeyboardButton]] = []

    for it in catalog_items:
        did = int(it["id"])
        title = (it.get("title") or "").strip()
        path = (it.get("path") or "").strip()
        chunks = int(it.get("chunks") or 0)
        label = title if title else path
        label = label if len(label) <= 40 else (label[:37] + "...")
        rows.append(
            [InlineKeyboardButton(f"{_badge(attached_map, did)} {did} · {chunks} · {label}",
                                  callback_data=f"{CB_PREFIX}:{page}:{did}:toggle")]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PREFIX}:{page-1}:0:page"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data=f"{CB_PREFIX}:{page}:0:noop"))
    if page < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PREFIX}:{page+1}:0:page"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("Закрыть", callback_data=f"{CB_PREFIX}:{page}:0:close")])

    text = (
        f"Выбор документов для диалога #{dialog_id}\n"
        f"Легенда: ✅ включён, ➖ исключён, ⬜ не подключён\n"
        f"Нажмите на документ, чтобы переключить состояние."
    )
    return text, InlineKeyboardMarkup(rows)


async def kb_select_show(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1) -> None:
    ds: DialogService = context.bot_data.get("svc_dialog")
    dkb: DialogKBService = context.bot_data.get("svc_dialog_kb")
    kb_repo: KBRepo = context.bot_data.get("repo_kb")

    if not ds or not dkb or not kb_repo or not update.effective_user:
        return

    d = ds.get_active_dialog(update.effective_user.id)
    if not d:
        await update.effective_message.reply_text("⚠️ Активный диалог не найден. Используйте /dialogs.")
        return

    attached = dkb.list_attached(d.id)
    attached_map = {int(x["document_id"]): bool(x["is_enabled"]) for x in attached}

    page_size = 10
    items, total = kb_repo.catalog(page=page, page_size=page_size, search="")

    text, markup = _render_page(
        dialog_id=d.id,
        page=page,
        page_size=page_size,
        catalog_items=items,
        total=total,
        attached_map=attached_map,
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return

    parts = q.data.split(":")
    if len(parts) != 4 or parts[0] != CB_PREFIX:
        return

    await q.answer()

    page = int(parts[1])
    doc_id = int(parts[2])
    action = parts[3]

    ds: DialogService = context.bot_data.get("svc_dialog")
    dkb: DialogKBService = context.bot_data.get("svc_dialog_kb")

    if not ds or not dkb or not update.effective_user:
        return

    d = ds.get_active_dialog(update.effective_user.id)
    if not d:
        await q.edit_message_text("⚠️ Активный диалог не найден. Используйте /dialogs.")
        return

    if action == "toggle" and doc_id > 0:
        # best-practice toggle: attach/enabled -> disable -> enable
        dkb.toggle_attach_enabled(d.id, doc_id)
        await kb_select_show(update, context, page=page)
        return

    if action == "page":
        await kb_select_show(update, context, page=page)
        return

    if action == "close":
        await q.edit_message_text("Готово. Используйте /kb list или /kb stats dialog.")
        return

    # noop
    return


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^kbsel:"))
