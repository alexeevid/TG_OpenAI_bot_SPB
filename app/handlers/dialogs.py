from __future__ import annotations

from html import escape
from math import ceil
from typing import List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from app.db.repo_dialogs import DialogsRepo

BTN_NEW = "dlg:new"
BTN_PAGE_PREV = "dlg:prev"
BTN_PAGE_NEXT = "dlg:next"
BTN_OPEN_PREFIX = "dlg:open:"
BTN_RENAME_PREFIX = "dlg:rename:"
BTN_DELETE_PREFIX = "dlg:delete:"


def _display_title_mask(d) -> str:
    title = (getattr(d, "title", None) or "").strip()
    if title:
        return title[:64]
    return f"Диалог #{getattr(d, 'id', '?')}"


def _build_keyboard(items: List[Tuple[int, str]], page: int, pages_total: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for dialog_id, title in items:
        kb.append(
            [
                InlineKeyboardButton(title, callback_data=f"{BTN_OPEN_PREFIX}{dialog_id}"),
                InlineKeyboardButton("✏️", callback_data=f"{BTN_RENAME_PREFIX}{dialog_id}"),
                InlineKeyboardButton("🗑", callback_data=f"{BTN_DELETE_PREFIX}{dialog_id}"),
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=BTN_PAGE_PREV))
    nav.append(InlineKeyboardButton(f"{page}/{pages_total}", callback_data="noop"))
    if page < pages_total:
        nav.append(InlineKeyboardButton("➡️", callback_data=BTN_PAGE_NEXT))
    kb.append(nav)

    kb.append([InlineKeyboardButton("➕ Новый диалог", callback_data=BTN_NEW)])
    return InlineKeyboardMarkup(kb)


async def _render(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user:
        return

    u = repo.ensure_user(str(update.effective_user.id))

    page = int(context.user_data.get("dialogs_page", 1))
    page_size = 8

    total = repo.count_dialogs(u.id)
    pages_total = max(1, ceil(total / page_size))

    # clamp page
    page = max(1, min(page, pages_total))
    context.user_data["dialogs_page"] = page

    offset = (page - 1) * page_size
    items = repo.list_dialogs_page(u.id, limit=page_size, offset=offset)

    kb_items: List[Tuple[int, str]] = [(d.id, _display_title_mask(d)) for d in items]
    markup = _build_keyboard(kb_items, page, pages_total)

    text = (
        "📚 <b>Диалоги</b>\n"
        "Выберите диалог или создайте новый.\n\n"
        "<i>✏️ — переименовать, 🗑 — удалить</i>"
    )

    msg = update.effective_message
    if edit and update.callback_query and update.callback_query.message:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        if msg:
            await msg.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["dialogs_page"] = 1
    await _render(update, context, edit=False)


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user or not update.callback_query:
        return

    data = update.callback_query.data or ""
    u = repo.ensure_user(str(update.effective_user.id))

    if data == "noop":
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
        repo.new_dialog(u.id, title=None, settings={})
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_OPEN_PREFIX):
        dialog_id = int(data[len(BTN_OPEN_PREFIX) :])
        repo.set_active_dialog(u.id, dialog_id)
        await update.callback_query.answer("Активный диалог выбран")
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_RENAME_PREFIX):
        dialog_id = int(data[len(BTN_RENAME_PREFIX) :])
        context.user_data["rename_dialog_id"] = dialog_id
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "Введите новое название диалога одним сообщением (до 80 символов)."
        )
        return

    if data.startswith(BTN_DELETE_PREFIX):
        dialog_id = int(data[len(BTN_DELETE_PREFIX) :])
        repo.delete_dialog(dialog_id)
        await update.callback_query.answer("Удалено")
        await _render(update, context, edit=True)
        return

    await update.callback_query.answer()


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user or not update.message:
        return

    dialog_id = context.user_data.pop("rename_dialog_id", None)
    if not dialog_id:
        return

    title = (update.message.text or "").strip()[:80]
    title = escape(title)
    if not title:
        await update.message.reply_text("Пустое название не принято.")
        return

    repo.rename_dialog(int(dialog_id), title=title)
    await update.message.reply_text("Готово.")
    await _render(update, context, edit=False)


def build_handlers() -> List:
    return [
        CommandHandler("dialogs", cmd_dialogs),
        CallbackQueryHandler(on_cb, pattern=r"^(dlg:.*|noop)$"),
        CommandHandler("rename", cmd_rename),
    ]


def register(app) -> None:
    """
    Совместимость с main.py: dialogs.register(app)
    """
    for h in build_handlers():
        app.add_handler(h)
