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
from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService

STATE_RENAME = 1
PAGE_SIZE = 5

UD_PAGE_KEY = "dialogs_page"

CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"
CB_CANCEL = "dlg:cancel"
CB_NOOP = "dlg:noop"
CB_PAGE = "dlg:page"  # dlg:page:<n>


def _parse_cb(data: str) -> Tuple[str, Optional[int]]:
    parts = (data or "").split(":")
    if len(parts) >= 2 and parts[0] == "dlg":
        action = ":".join(parts[:2])
        did = None
        if len(parts) >= 3:
            try:
                did = int(parts[2])
            except Exception:
                did = None
        return action, did
    return data, None


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def _prefix_from_created_or_updated(d) -> Optional[str]:
    dt = getattr(d, "created_at", None) or getattr(d, "updated_at", None)
    if not dt:
        return None
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _truncate(s: str, n: int = 60) -> str:
    s = (s or "").strip()
    if not s:
        return "Диалог"
    return s if len(s) <= n else (s[: n - 1] + "…")


def _display_title_mask(d) -> str:
    raw = (getattr(d, "title", "") or "").strip()
    prefix = _prefix_from_created_or_updated(d)

    if not prefix:
        return _truncate(raw, 80) if raw else "Диалог"

    if raw and len(raw) >= 11 and raw[:10] == prefix and raw[10:11] == "_":
        return _truncate(raw, 80)

    name = _truncate(raw, 60) if raw else "Диалог"
    return f"{prefix}_{name}"


def _ensure_mask_for_storage(d, user_part: str) -> str:
    user_part = (user_part or "").strip()
    if not user_part:
        user_part = "Диалог"

    prefix = _prefix_from_created_or_updated(d)
    if not prefix:
        return user_part[:80]

    if len(user_part) >= 11 and user_part[:10] == prefix and user_part[10:11] == "_":
        return user_part[:80]

    return f"{prefix}_{user_part}"[:80]


def _safe_page(n: int, pages_total: int) -> int:
    if pages_total <= 0:
        return 1
    if n < 1:
        return 1
    if n > pages_total:
        return pages_total
    return n


def _build_keyboard(dialogs, active_id: Optional[int], page: int, pages_total: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        is_active = bool(active_id and d.id == active_id)
        title = _display_title_mask(d)
        title_btn = f"✅ {d.id} — {title}" if is_active else f"{d.id} — {title}"

        kb.append([InlineKeyboardButton(text=title_btn, callback_data=f"{CB_OPEN}:{d.id}")])

        updated_s = _fmt_dt(getattr(d, "updated_at", None))
        kb.append([
            InlineKeyboardButton(text=f"изм.: {updated_s}", callback_data=f"{CB_NOOP}:{d.id}"),
            InlineKeyboardButton("✏️", callback_data=f"{CB_RENAME}:{d.id}"),
            InlineKeyboardButton("🗑", callback_data=f"{CB_DELETE}:{d.id}"),
        ])

    # Навигация страниц
    nav_row: List[InlineKeyboardButton] = []
    if pages_total > 1:
        prev_p = page - 1
        next_p = page + 1
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
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        if update.message:
            await update.message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not ds or not repo or not update.effective_user:
        if update.message:
            await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return

    u = repo.ensure_user(str(update.effective_user.id))

    total = repo.count_dialogs(u.id)
    pages_total = max(1, ceil(total / PAGE_SIZE))

    page = int(context.user_data.get(UD_PAGE_KEY, 1))
    page = _safe_page(page, pages_total)
    context.user_data[UD_PAGE_KEY] = page

    offset = (page - 1) * PAGE_SIZE
    dialogs = repo.list_dialogs_page(u.id, limit=PAGE_SIZE, offset=offset)

    active = repo.get_active_dialog(u.id)
    active_id = active.id if active else None

    if not dialogs:
        text = "<b>Диалоги</b>\nДиалогов пока нет. Нажмите «➕ Новый»."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый", callback_data=f"{CB_NEW}:0")]])
        if update.message:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        elif update.callback_query and edit:
            await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    text = "<b>Диалоги</b>\n"
    text += f"Активный: <b>{escape(str(active_id))}</b>\n"
    text += f"Страница: <b>{page}/{pages_total}</b>"

    kb = _build_keyboard(dialogs, active_id, page, pages_total)

    if update.callback_query and edit:
        await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # при ручном вызове открываем 1-ю страницу (последние)
    context.user_data[UD_PAGE_KEY] = 1
    await _render(update, context, edit=False)


async def cb_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not update.effective_user:
        return

    await q.answer()

    ds: DialogService = context.bot_data.get("svc_dialog")
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not ds or not repo:
        await q.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return

    action, did = _parse_cb(q.data or "")
    u = repo.ensure_user(str(update.effective_user.id))

    if action == CB_NOOP:
        return

    if action == CB_CLOSE:
        await q.message.edit_reply_markup(reply_markup=None)
        return

    if action == CB_REFRESH:
        await _render(update, context, edit=True)
        return

    if action == CB_PAGE:
        # did тут = номер страницы
        page = int(did or 1)
        context.user_data[UD_PAGE_KEY] = page
        await _render(update, context, edit=True)
        return

    if action == CB_NEW:
        ds.new_dialog(update.effective_user.id, title="Диалог")
        context.user_data[UD_PAGE_KEY] = 1
        await _render(update, context, edit=True)
        return

    if action == CB_CANCEL:
        context.user_data.pop("rename_dialog_id", None)
        await _render(update, context, edit=True)
        return ConversationHandler.END

    if did is None:
        await _render(update, context, edit=True)
        return

    d = repo.get_dialog_for_user(did, u.id)
    if not d:
        await q.message.reply_text("⛔ Диалог не найден или недоступен.")
        return

    if action == CB_OPEN:
        repo.set_active_dialog(u.id, did)
        await _render(update, context, edit=True)
        return

    if action == CB_DELETE:
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

    if action == CB_DELETE_OK:
        repo.delete_dialog(did)
        await q.message.reply_text("🗑 Диалог удалён.")
        await _render(update, context, edit=True)
        return

    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = did
        await q.message.reply_text("Введите новое имя диалога (только пользовательская часть).")
        return STATE_RENAME


async def rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return ConversationHandler.END

    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo:
        await update.message.reply_text("⚠️ Репозиторий диалогов не настроен.")
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
        context.user_data.pop("rename_dialog_id", None)
        return ConversationHandler.END

    title_to_store = _ensure_mask_for_storage(d, raw)
    repo.rename_dialog(int(did), title_to_store)

    context.user_data.pop("rename_dialog_id", None)
    await update.message.reply_text("✅ Переименовано.")
    await _render(update, context, edit=False)
    return ConversationHandler.END


def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    app.add_handler(CallbackQueryHandler(
        cb_dialogs,
        pattern=r"^dlg:(open|rename|delete|delete_ok|new|refresh|close|cancel|noop|page):"
    ))

    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive)]},
        fallbacks=[CallbackQueryHandler(cb_dialogs, pattern=r"^dlg:cancel:0$")],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv)
