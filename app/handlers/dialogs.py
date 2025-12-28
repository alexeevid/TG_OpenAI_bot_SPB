from __future__ import annotations

from dataclasses import dataclass
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

PAGE_SIZE = 8  # компактно, чтобы не «обрезало» экран

CB_PAGE = "dlg:page"
CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"
CB_CANCEL = "dlg:cancel"


def _short(s: str, n: int = 26) -> str:
    s = (s or "").strip()
    if not s:
        return "(без названия)"
    return s if len(s) <= n else (s[: n - 1] + "…")


def _fmt_dt(dt) -> str:
    # dt — обычно naive datetime из БД
    try:
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return "-"


def _parse_cb(data: str) -> Tuple[str, Optional[int]]:
    # формат: prefix[:id]
    # примеры: dlg:open:59, dlg:page:1
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


def _build_keyboard(dialogs, active_id: Optional[int], page: int, pages: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        mark = "✅ " if active_id and d.id == active_id else ""
        title = _short(getattr(d, "title", "") or "")
        kb.append(
            [
                InlineKeyboardButton(text=f"{mark}{d.id}: {title}", callback_data=f"{CB_OPEN}:{d.id}"),
                InlineKeyboardButton(text="✏️", callback_data=f"{CB_RENAME}:{d.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"{CB_DELETE}:{d.id}"),
            ]
        )

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

    # Достаём пользователя и активный диалог через repo (чтобы иметь user_id)
    u = repo.ensure_user(str(update.effective_user.id))
    dialogs_all = repo.list_dialogs(u.id, limit=200)

    if not dialogs_all:
        if update.message:
            await update.message.reply_text("Диалогов пока нет. Используйте ➕ Новый или /reset для создания.")
        return

    active = repo.get_active_dialog(u.id)
    active_id = active.id if active else None

    pages = max(1, (len(dialogs_all) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    context.user_data["dialogs_page"] = page

    start = page * PAGE_SIZE
    dialogs = dialogs_all[start : start + PAGE_SIZE]

    # Текст — компактный, но информативный
    header = f"*Диалоги*  (стр. {page+1}/{pages})\n"
    lines = []
    for d in dialogs:
        title = (getattr(d, "title", "") or "").strip() or "(без названия)"
        mark = "✅" if active_id and d.id == active_id else "•"
        lines.append(
            f"{mark} *{d.id}* — {title}\n"
            f"   _создан:_ `{_fmt_dt(getattr(d, 'created_at', None))}`  _изм.:_ `{_fmt_dt(getattr(d, 'updated_at', None))}`"
        )
    text = header + "\n".join(lines)

    kb = _build_keyboard(dialogs, active_id, page, pages)

    if update.callback_query and edit:
        await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Единственная команда управления диалогами
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

    # Защита: доступ только к своим диалогам
    d = repo.get_dialog_for_user(dialog_id, u.id)
    if not d:
        await query.message.reply_text("⛔ Диалог не найден или недоступен.")
        return

    if action == CB_OPEN:
        repo.set_active_dialog(u.id, dialog_id)
        await query.message.reply_text(f"⭐ Активный диалог: {dialog_id}")
        await _render_dialogs(update, context, page=int(context.user_data.get('dialogs_page', 0) or 0), edit=True)
        return

    if action == CB_DELETE:
        # Подтверждение удаления
        title = (d.title or "").strip() or "(без названия)"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(text="✅ Удалить", callback_data=f"{CB_DELETE_OK}:{dialog_id}"),
                    InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0"),
                ]
            ]
        )
        await query.message.reply_text(f"Удалить диалог *{dialog_id}* — {title}?", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    if action == CB_DELETE_OK:
        repo.delete_dialog(dialog_id)
        await query.message.reply_text("🗑 Диалог удалён.")
        # Если активный был удалён — сервис сам создаст новый при первом обращении, но меню покажем актуально
        await _render_dialogs(update, context, page=0, edit=True)
        return

    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = dialog_id
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(text="↩️ Отмена", callback_data=f"{CB_CANCEL}:0")]])
        await query.message.reply_text(
            f"Введите новое имя для диалога *{dialog_id}* (или отправьте пустое сообщение, чтобы очистить название).",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return


async def _cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Универсальная отмена (и для inline, и для состояния)
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

    title = (update.message.text or "").strip()
    if len(title) > 80:
        await update.message.reply_text("Название слишком длинное. Максимум 80 символов.")
        return STATE_RENAME

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(dialog_id), u.id)
    if not d:
        await update.message.reply_text("⛔ Диалог не найден или недоступен.")
        return ConversationHandler.END

    repo.rename_dialog(int(dialog_id), title)
    context.user_data.pop("rename_dialog_id", None)

    await update.message.reply_text("✏️ Название обновлено.")
    page = int(context.user_data.get("dialogs_page", 0) or 0)
    await _render_dialogs(update, context, page=page, edit=False)
    return ConversationHandler.END


def register(app: Application) -> None:
    # 1) Единая команда управления диалогами
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    # 2) Callback-управление меню
    app.add_handler(CallbackQueryHandler(_cb_dialogs, pattern=r"^dlg:(page|open|rename|delete|delete_ok|new|refresh|close):"))

    # 3) Переименование — как диалоговое состояние (не конфликтует с text handler, если стоит раньше)
    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(_cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _rename_receive)]},
        fallbacks=[CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"), CommandHandler("cancel", _cb_cancel)],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv)

    # Отмена для удаления/прочих действий (когда пользователь нажал отмену)
    app.add_handler(CallbackQueryHandler(_cb_cancel, pattern=r"^dlg:cancel:0$"))
