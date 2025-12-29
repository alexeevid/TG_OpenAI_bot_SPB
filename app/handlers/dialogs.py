from __future__ import annotations

from typing import List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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

PAGE_SIZE = 8  # чтобы не “съедало” экран

CB_PAGE = "dlg:page"
CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"
CB_CANCEL = "dlg:cancel"


def _fmt_date_prefix(dt) -> str:
    """YYYY-MM-DD по created_at; если dt нет — '0000-00-00'."""
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def _short(s: str, n: int = 42) -> str:
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
    """
    UI-имя диалога: YYYY-MM-DD_<Название>.
    Если title пустой — YYYY-MM-DD_Диалог
    """
    prefix = _fmt_date_prefix(getattr(d, "created_at", None))
    base = _short(getattr(d, "title", "") or "", n=48)
    return f"{prefix}_{base}"


def _normalize_title_for_storage(d, user_input: str) -> str:
    """
    Хранение title: всегда YYYY-MM-DD_<имя>.
    Если пользователь ввёл уже с префиксом даты — не дублируем.
    """
    prefix = _fmt_date_prefix(getattr(d, "created_at", None))
    name = (user_input or "").strip()

    # Разрешаем “очистить” имя: тогда будет YYYY-MM-DD_Диалог
    if not name:
        return ""

    # Если пользователь сам ввёл YYYY-MM-DD_..., оставляем как есть
    if len(name) >= 11 and name[:10] == prefix and name[10:11] == "_":
        # но ограничим длину хранения
        return name[:80]

    # Иначе добавляем префикс
    return f"{prefix}_{name}"[:80]


def _build_keyboard(dialogs, active_id: Optional[int], page: int, pages: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        is_active = bool(active_id and d.id == active_id)
        mark = "✅ " if is_active else ""
        title = _display_title(d)

        # 1-я строка: выбор диалога на всю ширину
        kb.append([
            InlineKeyboardButton(
                text=f"{mark}{d.id}: {title}",
                callback_data=f"{CB_OPEN}:{d.id}",
            )
        ])

        # 2-я строка: действия
        kb.append([
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"{CB_RENAME}:{d.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{CB_DELETE}:{d.id}"),
        ])

    # Навигация/действия снизу
    nav: List[InlineKeyboardButton] = []
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                text="⏮" if page > 0 else "·",
                callback_data=f"{CB_PAGE}:{page-1}" if page > 0 else f"{CB_REFRESH}:0",
            )
        )
        nav.append(
            InlineKeyboardButton(
                text=f"{page+1}/{pages}",
                callback_data=f"{CB_REFRESH}:0",
            )
        )
        nav.append(
            InlineKeyboardButton(
                text="⏭" if page < pages - 1 else "·",
                callback_data=f"{CB_PAGE}:{page+1}" if page < pages - 1 else f"{CB_REFRESH}:0",
            )
        )
    nav.append(InlineKeyboardButton(text="➕ Новый", callback_data=f"{CB_NEW}:0"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"{CB_REFRESH}:0"))
    kb.append(nav)

    kb.append([InlineKeyboardButton(text="Закрыть", callback_data=f"{CB_CLOSE}:0")])

    return InlineKeyboardMarkup(kb)


async def _render_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE, *, page: int = 0, edit: bool = False) -> None:
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
    dialogs_all = repo.list_dialogs(u.id, limit=200)

    if not dialogs_all:
        if update.message:
            await update.message.reply_text("Диалогов пока нет. Нажмите ➕ Новый или используйте /reset.")
        return

    active = repo.get_active_dialog(u.id)
    active_id = active.id if active else None

    pages = max(1, (len(dialogs_all) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    context.user_data["dialogs_page"] = page

    start = page * PAGE_SIZE
    dialogs = dialogs_all[start : start + PAGE_SIZE]

    # ВАЖНО: убрали верхний дублирующий список — оставляем только заголовок
    if active_id:
        text = f"*Диалоги* (стр. {page+1}/{pages})\nАктивный: *{active_id}*"
    else:
        text = f"*Диалоги* (стр. {page+1}/{pages})\nАктивный: _не выбран_"

    kb = _build_keyboard(dialogs, active_id, page, pages)

    if update.callback_query and edit:
        await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render_dialogs(update, context, page=0, edit=False)


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
        page = int(context.user_data.get("dialogs_page", 0) or 0)
        await _render_dialogs(update, context, page=page, edit=True)
        return

    if action == CB_PAGE:
        page = dialog_id if dialog_id is not None else 0
        await _render_dialogs(update, context, page=page, edit=True)
        return

    if action == CB_NEW:
        ds.new_dialog(update.effective_user.id, title="")
        await _render_dialogs(update, context, page=0, edit=True)
        return

    if dialog_id is None:
        await _render_dialogs(update, context, page=int(context.user_data.get("dialogs_page", 0) or 0), edit=True)
        return

    d = repo.get_dialog_for_user(dialog_id, u.id)
    if not d:
        await query.message.reply_text("⛔ Диалог не найден или недоступен.")
        return

    if action == CB_OPEN:
        repo.set_active_dialog(u.id, dialog_id)
        await query.message.reply_text(f"⭐ Активный диалог: {dialog_id}")
        await _render_dialogs(update, context, page=int(context.user_data.get("dialogs_page", 0) or 0), edit=True)
        return

    if action == CB_DELETE:
        title_ui = _display_title(d)
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(text="✅ Удалить", callback_data=f"{CB_DELETE_OK}:{dialog_id}"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0"),
            ]]
        )
        await query.message.reply_text(
            f"Удалить диалог *{dialog_id}*?\n_{title_ui}_",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == CB_DELETE_OK:
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        await _render_dialogs(update, context, page=0, edit=True)
        return

    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = dialog_id
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0")]])
        prefix = _fmt_date_prefix(getattr(d, "created_at", None))
        await query.message.reply_text(
            "Введите новое имя диалога.\n"
            f"Формат будет сохранён как: `{prefix}_<имя>`\n"
            "Можно отправить пустое сообщение, чтобы очистить пользовательскую часть названия.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return


async def _cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.pop("rename_dialog_id", None)
    page = int(context.user_data.get("dialogs_page", 0) or 0)
    await _render_dialogs(update, context, page=page, edit=bool(update.callback_query))
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

    title_to_store = _normalize_title_for_storage(d, raw)
    repo.rename_dialog(int(dialog_id), title_to_store)

    context.user_data.pop("rename_dialog_id", None)
    await update.message.reply_text("✏️ Название обновлено.")

    page = int(context.user_data.get("dialogs_page", 0) or 0)
    await _render_dialogs(update, context, page=page, edit=False)
    return ConversationHandler.END


def register(app: Application) -> None:
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    # общий callback
    app.add_handler(CallbackQueryHandler(
        _cb_dialogs,
        pattern=r"^dlg:(page|open|rename|delete|delete_ok|new|refresh|close):"
    ))

    # rename flow
    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _rename_receive)]},
        fallbacks=[CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"), CommandHandler("cancel", _cb_cancel)],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv)

    # cancel fallback
    app.add_handler(CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"))
