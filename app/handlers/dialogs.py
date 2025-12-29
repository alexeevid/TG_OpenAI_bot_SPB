from __future__ import annotations

from html import escape
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

SHOW_LIMIT = 5  # показываем 5 последних диалогов (по updated_at DESC)

CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"
CB_CANCEL = "dlg:cancel"


def _dt_best(d):
    return getattr(d, "created_at", None) or getattr(d, "updated_at", None)


def _fmt_date_prefix(d) -> str:
    dt = _dt_best(d)
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def _fmt_dt(dt) -> str:
    try:
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return "-"


def _short(s: str, n: int = 60) -> str:
    s = (s or "").strip()
    if not s:
        return "Диалог"
    return s if len(s) <= n else (s[: n - 1] + "…")


def _parse_cb(data: str) -> Tuple[str, Optional[int]]:
    parts = (data or "").split(":")
    if len(parts) >= 2 and parts[0] == "dlg":
        action = ":".join(parts[:2])  # dlg:open
        dialog_id = None
        if len(parts) >= 3:
            try:
                dialog_id = int(parts[2])
            except Exception:
                dialog_id = None
        return action, dialog_id
    return data, None


def _display_title(d) -> str:
    prefix = _fmt_date_prefix(d)
    raw = (getattr(d, "title", "") or "").strip()

    # Если уже хранится YYYY-MM-DD_... — не дублируем
    if raw and len(raw) >= 11 and raw[:10] == prefix and raw[10:11] == "_":
        return _short(raw, n=70)

    base = _short(raw, n=60)
    return f"{prefix}_{base}"


def _build_keyboard(dialogs, active_id: Optional[int]) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        is_active = bool(active_id and d.id == active_id)

        kb.append([
            InlineKeyboardButton(
                text=("✅ Активный" if is_active else "Выбрать") + f" #{d.id}",
                callback_data=f"{CB_OPEN}:{d.id}",
            )
        ])

        kb.append([
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"{CB_RENAME}:{d.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CB_DELETE}:{d.id}"),
        ])

    kb.append([
        InlineKeyboardButton(text="➕ Новый", callback_data=f"{CB_NEW}:0"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{CB_REFRESH}:0"),
    ])

    kb.append([InlineKeyboardButton(text="Закрыть", callback_data=f"{CB_CLOSE}:0")])

    return InlineKeyboardMarkup(kb)


async def _render_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False) -> None:
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

    dialogs = repo.list_dialogs(u.id, limit=SHOW_LIMIT)

    active = repo.get_active_dialog(u.id)
    active_id = active.id if active else None

    if not dialogs:
        msg = "Диалогов пока нет. Нажмите «➕ Новый»."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый", callback_data=f"{CB_NEW}:0")]])
        if update.message:
            await update.message.reply_text(msg, reply_markup=kb)
        elif update.callback_query and edit:
            await update.callback_query.message.edit_text(msg, reply_markup=kb)
        return

    # HTML-текст: безопасно экранируем всё, что может прийти из БД
    lines = ["<b>Диалоги (последние 5)</b>"]
    lines.append(f"Активный: <b>{escape(str(active_id))}</b>" if active_id else "Активный: <i>не выбран</i>")
    lines.append("")

    for d in dialogs:
        mark = "✅" if active_id and d.id == active_id else "•"
        title_ui = escape(_display_title(d))

        created_dt = getattr(d, "created_at", None) or getattr(d, "updated_at", None)
        updated_dt = getattr(d, "updated_at", None) or getattr(d, "created_at", None)

        created_s = escape(_fmt_dt(created_dt))
        updated_s = escape(_fmt_dt(updated_dt))

        lines.append(f"{mark} <b>{d.id}</b> — {title_ui}")
        lines.append(f"<i>   создан:</i> <code>{created_s}</code>   <i>изм.:</i> <code>{updated_s}</code>")

    text = "\n".join(lines)
    kb = _build_keyboard(dialogs, active_id)

    if update.callback_query and edit:
        await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render_dialogs(update, context, edit=False)


async def _cb_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()

    ds: DialogService = context.bot_data.get("svc_dialog")
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not ds or not repo:
        await query.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return

    action, dialog_id = _parse_cb(query.data or "")
    u = repo.ensure_user(str(update.effective_user.id))

    if action == CB_CLOSE:
        await query.message.edit_reply_markup(reply_markup=None)
        return

    if action == CB_REFRESH:
        await _render_dialogs(update, context, edit=True)
        return

    if action == CB_NEW:
        ds.new_dialog(update.effective_user.id, title="")
        await _render_dialogs(update, context, edit=True)
        return

    if action == CB_CANCEL:
        context.user_data.pop("rename_dialog_id", None)
        await _render_dialogs(update, context, edit=True)
        return ConversationHandler.END

    if dialog_id is None:
        await _render_dialogs(update, context, edit=True)
        return

    d = repo.get_dialog_for_user(dialog_id, u.id)
    if not d:
        await query.message.reply_text("⛔ Диалог не найден или недоступен.")
        return

    if action == CB_OPEN:
        repo.set_active_dialog(u.id, dialog_id)
        await query.message.reply_text(f"⭐ Активный диалог: {dialog_id}")
        await _render_dialogs(update, context, edit=True)
        return

    if action == CB_DELETE:
        title_ui = escape(_display_title(d))
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(text="✅ Удалить", callback_data=f"{CB_DELETE_OK}:{dialog_id}"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0"),
            ]]
        )
        await query.message.reply_text(
            f"Удалить диалог <b>{dialog_id}</b>?\n<i>{title_ui}</i>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if action == CB_DELETE_OK:
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        await _render_dialogs(update, context, edit=True)
        return

    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = dialog_id
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0")]])
        prefix = _fmt_date_prefix(d)
        await query.message.reply_text(
            "Введите новое имя диалога.\n"
            f"Формат будет: <code>{escape(prefix)}_&lt;имя&gt;</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return STATE_RENAME


async def _cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.pop("rename_dialog_id", None)
    await _render_dialogs(update, context, edit=bool(update.callback_query))
    return ConversationHandler.END


async def _rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return ConversationHandler.END

    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo:
        await update.message.reply_text("⚠️ Репозиторий диалогов не настроен.")
        return ConversationHandler.END

    dialog_id = context.user_data.get("rename_dialog_id")
    if not dialog_id:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    if len(raw) > 80:
        await update.message.reply_text("Название слишком длинное. Максимум 80 символов.")
        return STATE_RENAME

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(dialog_id), u.id)
    if not d:
        await update.message.reply_text("⛔ Диалог не найден или недоступен.")
        return ConversationHandler.END

    repo.rename_dialog(int(dialog_id), raw)
    context.user_data.pop("rename_dialog_id", None)

    await update.message.reply_text("✏️ Название обновлено.")
    await _render_dialogs(update, context, edit=False)
    return ConversationHandler.END


def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    app.add_handler(CallbackQueryHandler(
        _cb_dialogs,
        pattern=r"^dlg:(open|rename|delete|delete_ok|new|refresh|close|cancel):"
    ))

    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _rename_receive)]},
        fallbacks=[CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"), CommandHandler("cancel", _cb_cancel)],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv)

    app.add_handler(CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"))
