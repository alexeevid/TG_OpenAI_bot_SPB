# app/handlers/dialogs.py
from __future__ import annotations

import re
from datetime import datetime
from math import ceil
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.db.repo_dialogs import DialogsRepo

log_prefix = "dialogs"

# --- callbacks ---
BTN_NEW = "dlg:new"
BTN_PAGE_PREV = "dlg:prev"
BTN_PAGE_NEXT = "dlg:next"
BTN_OPEN_PREFIX = "dlg:open:"
BTN_RENAME_PREFIX = "dlg:rename:"
BTN_DELETE_PREFIX = "dlg:delete:"
BTN_CANCEL_RENAME = "dlg:cancel_rename"
NOOP = "noop"

# --- UI constants ---
PAGE_SIZE = 5
TITLE_MAX = 64

# --- conversation states ---
RENAME_WAIT_TEXT = 1
RENAME_TIMEOUT_SEC = 60

DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(.+)$", re.UNICODE)


def _fmt_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d")


def _strip_date_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return ""
    m = DATE_PREFIX_RE.match(t)
    if m:
        return (m.group(1) or "").strip()
    return t


def _masked_title(created_at: Optional[datetime], raw_title: str) -> str:
    """
    Всегда возвращает: YYYY-MM-DD_<Имя>
    Имя берём из title (без даты), иначе "Новый диалог"
    """
    name = _strip_date_prefix(raw_title)
    if not name:
        name = "Новый диалог"

    date_part = _fmt_date(created_at)
    out = f"{date_part}_{name}"

    if len(out) > TITLE_MAX:
        out = out[: TITLE_MAX - 1] + "…"
    return out


def _build_keyboard(dialogs, *, active_dialog_id: Optional[int], page: int, pages_total: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        did = int(getattr(d, "id", 0))
        is_active = active_dialog_id == did

        created_at = getattr(d, "created_at", None)
        title = _masked_title(created_at, getattr(d, "title", "") or "")

        # 1) имя — одна строка
        kb.append(
            [
                InlineKeyboardButton(
                    ("✅ " if is_active else "") + title,
                    callback_data=f"{BTN_OPEN_PREFIX}{did}",
                )
            ]
        )

        # 2) снизу: дата изменения + ✏️ + 🗑 (в одной строке)
        # дата изменения берётся из updated_at (если None — показываем created_at)
        updated_at = getattr(d, "updated_at", None) or created_at
        kb.append(
            [
                InlineKeyboardButton(f"🕒 {_fmt_date(updated_at)}", callback_data=NOOP),
                InlineKeyboardButton("✏️", callback_data=f"{BTN_RENAME_PREFIX}{did}"),
                InlineKeyboardButton("🗑", callback_data=f"{BTN_DELETE_PREFIX}{did}"),
            ]
        )

    # пагинация
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=BTN_PAGE_PREV))
    nav.append(InlineKeyboardButton(f"{page}/{pages_total}", callback_data=NOOP))
    if page < pages_total:
        nav.append(InlineKeyboardButton("➡️", callback_data=BTN_PAGE_NEXT))
    kb.append(nav)

    # отдельная кнопка Новый диалог
    kb.append([InlineKeyboardButton("➕ Новый диалог", callback_data=BTN_NEW)])

    return InlineKeyboardMarkup(kb)


async def _render(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    repo: DialogsRepo = context.application.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user:
        return

    u = repo.ensure_user(str(update.effective_user.id))
    active_dialog_id = getattr(u, "active_dialog_id", None)

    page = int(context.user_data.get("dialogs_page", 1))
    total = repo.count_dialogs(u.id)
    pages_total = max(1, ceil(total / PAGE_SIZE))
    page = max(1, min(page, pages_total))
    context.user_data["dialogs_page"] = page

    offset = (page - 1) * PAGE_SIZE
    dialogs = repo.list_dialogs_page(u.id, limit=PAGE_SIZE, offset=offset)

    markup = _build_keyboard(dialogs, active_dialog_id=active_dialog_id, page=page, pages_total=pages_total)

    # компактная шапка (без требований/инструкций)
    text = "📚 Диалоги"

    if edit and update.callback_query and update.callback_query.message:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=markup)
    else:
        if update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=markup)


# -------- commands --------
async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["dialogs_page"] = 1
    await _render(update, context, edit=False)


# -------- callbacks (non-conversation actions) --------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.application.bot_data.get("repo_dialogs")
    if not repo or not update.callback_query or not update.effective_user:
        return

    data = update.callback_query.data or ""
    u = repo.ensure_user(str(update.effective_user.id))

    if data == NOOP:
        await update.callback_query.answer()
        return

    if data == BTN_PAGE_PREV:
        context.user_data["dialogs_page"] = max(1, int(context.user_data.get("dialogs_page", 1)) - 1)
        await _render(update, context, edit=True)
        return

    if data == BTN_PAGE_NEXT:
        context.user_data["dialogs_page"] = int(context.user_data.get("dialogs_page", 1)) + 1
        await _render(update, context, edit=True)
        return

    if data == BTN_NEW:
        d = repo.new_dialog(u.id, title="", settings={})
        # имя по маске (дата создания + дефолт)
        masked = _masked_title(getattr(d, "created_at", None), "Новый диалог")
        repo.rename_dialog(d.id, masked)
        repo.set_active_dialog(u.id, d.id)

        await update.callback_query.answer("Создан")
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_OPEN_PREFIX):
        dialog_id = int(data[len(BTN_OPEN_PREFIX) :])
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            await update.callback_query.answer("Не найден", show_alert=True)
            return
        repo.set_active_dialog(u.id, d.id)
        await update.callback_query.answer("Активный")
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_DELETE_PREFIX):
        dialog_id = int(data[len(BTN_DELETE_PREFIX) :])
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            await update.callback_query.answer("Не найден", show_alert=True)
            return

        repo.delete_dialog(dialog_id)
        await update.callback_query.answer("Удалено")
        await _render(update, context, edit=True)
        return

    # rename обрабатывается ConversationHandler entrypoint (ниже)
    await update.callback_query.answer()


# -------- rename conversation --------
async def rename_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point on ✏️ callback.
    """
    repo: DialogsRepo = context.application.bot_data.get("repo_dialogs")
    if not repo or not update.callback_query or not update.effective_user:
        return ConversationHandler.END

    data = update.callback_query.data or ""
    u = repo.ensure_user(str(update.effective_user.id))

    dialog_id = int(data[len(BTN_RENAME_PREFIX) :])
    d = repo.get_dialog_for_user(dialog_id, u.id)
    if not d:
        await update.callback_query.answer("Не найден", show_alert=True)
        return ConversationHandler.END

    context.user_data["dlg_rename_id"] = dialog_id

    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Введите новое имя диалога одним сообщением (без даты).\n"
        "Отмена — кнопка ниже.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✖️ Отмена", callback_data=BTN_CANCEL_RENAME)]]
        ),
    )
    return RENAME_WAIT_TEXT


async def rename_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("dlg_rename_id", None)

    # может быть callback или команда
    if update.callback_query:
        await update.callback_query.answer("Отменено")
        if update.callback_query.message:
            await update.callback_query.message.reply_text("Ок, переименование отменено.")
    elif update.effective_message:
        await update.effective_message.reply_text("Ок, переименование отменено.")

    return ConversationHandler.END


async def rename_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    repo: DialogsRepo = context.application.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user or not update.effective_message:
        context.user_data.pop("dlg_rename_id", None)
        return ConversationHandler.END

    dialog_id = context.user_data.get("dlg_rename_id")
    if not dialog_id:
        return ConversationHandler.END

    new_name = (update.effective_message.text or "").strip()
    if not new_name:
        context.user_data.pop("dlg_rename_id", None)
        await update.effective_message.reply_text("Пустое имя — отменено.")
        return ConversationHandler.END

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(dialog_id), u.id)
    if not d:
        context.user_data.pop("dlg_rename_id", None)
        await update.effective_message.reply_text("Диалог не найден.")
        return ConversationHandler.END

    masked = _masked_title(getattr(d, "created_at", None), new_name)
    repo.rename_dialog(d.id, masked)

    context.user_data.pop("dlg_rename_id", None)

    await update.effective_message.reply_text("✅ Переименовано.")
    # перерисуем меню (не редактируем старое сообщение, просто показываем актуальное)
    await _render(update, context, edit=False)

    return ConversationHandler.END


async def rename_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Called when conversation times out.
    """
    context.user_data.pop("dlg_rename_id", None)
    if update.effective_message:
        await update.effective_message.reply_text("⏳ Время ожидания истекло. Переименование отменено.")
    return ConversationHandler.END


def register(app: Application) -> None:
    # /dialogs
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    # Основные кнопки меню (кроме rename)
    # rename перехватывается ConversationHandler entrypoint, чтобы не мешать тексту.
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^(dlg:(new|prev|next|open:|delete:).+|noop)$"))

    # Rename conversation:
    # - entry: callback dlg:rename:<id>
    # - state: next TEXT message becomes new name
    # - timeout: 60 sec
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rename_entry, pattern=r"^dlg:rename:\d+$")],
        states={
            RENAME_WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive_text),
                CallbackQueryHandler(rename_cancel, pattern=r"^dlg:cancel_rename$"),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(rename_cancel, pattern=r"^dlg:cancel_rename$"),
            CommandHandler("cancel", rename_cancel),
        ],
        conversation_timeout=RENAME_TIMEOUT_SEC,
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        per_message=False,
    )
    app.add_handler(conv)

    # Отмена по кнопке (если прилетела вне активного состояния — просто чистим)
    app.add_handler(CallbackQueryHandler(rename_cancel, pattern=r"^dlg:cancel_rename$"))
