from __future__ import annotations

from html import escape
from math import ceil
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..db.repo_dialogs import DialogsRepo

STATE_RENAME = 1

CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"
CB_CANCEL = "dlg:cancel"
CB_NOOP = "dlg:noop"
CB_PAGE = "dlg:page"


def _display_title_mask(d) -> str:
    # Показываем только пользовательскую часть названия (после "|"), если есть
    try:
        if d and getattr(d, "title", None):
            parts = str(d.title).split("|", 1)
            return parts[1].strip() if len(parts) == 2 else str(d.title).strip()
    except Exception:
        pass
    return ""


def _build_keyboard(items: List[Tuple[int, str]], page: int, pages_total: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for did, title in items:
        title_ui = title if title else f"Диалог {did}"
        kb.append([
            InlineKeyboardButton(f"🗂 {title_ui}", callback_data=f"{CB_OPEN}:{did}"),
            InlineKeyboardButton("✏️", callback_data=f"{CB_RENAME}:{did}"),
            InlineKeyboardButton("🗑", callback_data=f"{CB_DELETE}:{did}"),
        ])

    # pagination
    nav_row: List[InlineKeyboardButton] = []
    prev_p = max(1, page - 1)
    next_p = min(pages_total, page + 1)

    if pages_total > 1:
        if page > 1:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"{CB_PAGE}:{prev_p}"))
        nav_row.append(InlineKeyboardButton(f"{page}/{pages_total}", callback_data=f"{CB_NOOP}:0"))
        if page < pages_total:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"{CB_PAGE}:{next_p}"))
        kb.append(nav_row)

    kb.append([
        InlineKeyboardButton("➕ Новый", callback_data=f"{CB_NEW}:0"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"{CB_REFRESH}:0"),
    ])
    kb.append([InlineKeyboardButton("Закрыть", callback_data=f"{CB_CLOSE}:0")])

    return InlineKeyboardMarkup(kb)


async def _render(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user:
        return

    u = repo.ensure_user(str(update.effective_user.id))

    page = int(context.user_data.get("dialogs_page", 1))
    page_size = 8

    items, total = repo.list_dialogs(u.id, page=page, page_size=page_size)
    pages_total = max(1, ceil(total / page_size))

    # clamp page
    page = max(1, min(page, pages_total))
    context.user_data["dialogs_page"] = page

    kb_items: List[Tuple[int, str]] = [(d.id, _display_title_mask(d)) for d in items]
    markup = _build_keyboard(kb_items, page, pages_total)

    text = (
        "📚 <b>Диалоги</b>\n"
        "Выберите диалог или создайте новый.\n\n"
        "<i>✏️ — переименовать, 🗑 — удалить</i>"
    )

    msg = update.effective_message
    if edit and update.callback_query and update.callback_query.message:
        msg = update.callback_query.message

    if edit and msg:
        try:
            await msg.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except Exception:
            # fallback — если нельзя редактировать, отправим новое
            pass

    if msg:
        await msg.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render(update, context, edit=False)


async def cb_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not update.effective_user:
        return

    await q.answer()

    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo:
        await q.message.reply_text("⚠️ Repo не настроен.")
        return

    data = q.data or ""
    if ":" not in data:
        return

    action, arg = data.split(":", 1)
    did = int(arg) if arg.isdigit() else 0

    # open
    if action == CB_OPEN:
        u = repo.ensure_user(str(update.effective_user.id))
        d = repo.get_dialog_for_user(did, u.id)
        if not d:
            await q.message.reply_text("⛔ Диалог не найден или недоступен.")
            return

        repo.set_active_dialog(u.id, d.id)
        await q.message.reply_text(f"✅ Активен диалог <b>{d.id}</b>: <i>{escape(_display_title_mask(d))}</i>", parse_mode=ParseMode.HTML)
        return

    # delete ask
    if action == CB_DELETE:
        u = repo.ensure_user(str(update.effective_user.id))
        d = repo.get_dialog_for_user(did, u.id)
        if not d:
            await q.message.reply_text("⛔ Диалог не найден или недоступен.")
            return

        title_ui = escape(_display_title_mask(d))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Удалить", callback_data=f"{CB_DELETE_OK}:{did}"),
            InlineKeyboardButton("↩️ Отмена", callback_data=f"{CB_CANCEL}:0"),
        ]])
        await q.message.reply_text(
            f"Удалить диалог <b>{did}</b>?\n<i>{title_ui}</i>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    # delete ok
    if action == CB_DELETE_OK:
        repo.delete_dialog(did)
        await q.message.reply_text("🗑 Диалог удалён.")
        await _render(update, context, edit=True)
        return

    # rename entry (Conversation entry_point)
    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = did
        await q.message.reply_text("Введите новое имя диалога (только пользовательская часть).")
        return STATE_RENAME

    # new
    if action == CB_NEW:
        u = repo.ensure_user(str(update.effective_user.id))
        d = repo.create_dialog(u.id, title="Новый диалог")
        repo.set_active_dialog(u.id, d.id)
        await q.message.reply_text(f"➕ Создан и активирован диалог <b>{d.id}</b>.", parse_mode=ParseMode.HTML)
        await _render(update, context, edit=True)
        return

    # refresh
    if action == CB_REFRESH:
        await _render(update, context, edit=True)
        return

    # close
    if action == CB_CLOSE:
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    # cancel
    if action == CB_CANCEL:
        await _render(update, context, edit=True)
        return

    # page
    if action == CB_PAGE:
        try:
            page = int(arg)
        except Exception:
            page = 1
        context.user_data["dialogs_page"] = max(1, page)
        await _render(update, context, edit=True)
        return

    # noop
    return


async def rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user:
        return ConversationHandler.END

    did = context.user_data.get("rename_dialog_id")
    if not did:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    if len(raw) > 80:
        await update.message.reply_text("Название слишком длинное. Максимум 80 символов.")
        return STATE_RENAME

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(did), u.id)
    if not d:
        await update.message.reply_text("⛔ Диалог не найден или недоступен.")
        return ConversationHandler.END

    # сохраняем в формате "date | user title" если есть "|", иначе просто user title
    # (логика наработок сохраняется как в исходнике)
    title = getattr(d, "title", "") or ""
    if "|" in title:
        left, _ = title.split("|", 1)
        new_title = f"{left.strip()} | {raw}"
    else:
        new_title = raw

    repo.rename_dialog(d.id, new_title)
    context.user_data.pop("rename_dialog_id", None)

    await update.message.reply_text("✅ Переименовано.")
    await _render(update, context, edit=False)
    return ConversationHandler.END


async def rename_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Завершить режим переименования и вернуться к списку диалогов
    context.user_data.pop("rename_dialog_id", None)
    await _render(update, context, edit=True)
    return ConversationHandler.END


def register(app: Application) -> None:
    # Команда
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    # Conversation для переименования: должен иметь более высокий приоритет, чем общий CallbackQueryHandler
    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive)]},
        fallbacks=[CallbackQueryHandler(rename_cancel, pattern=r"^dlg:cancel:0$")],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv, group=0)

    # Общие callback-и диалогов (БЕЗ rename) — ниже по приоритету, чтобы не перехватывать entry_points ConversationHandler
    app.add_handler(
        CallbackQueryHandler(
            cb_dialogs,
            pattern=r"^dlg:(open|delete|delete_ok|new|refresh|close|cancel|noop|page):",
        ),
        group=1,
    )
