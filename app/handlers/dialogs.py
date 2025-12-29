from __future__ import annotations

from typing import List, Optional, Tuple

from telegram import (
    ForceReply,
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


# ---------------------------
# Конфигурация UI
# ---------------------------

SHOW_LIMIT = 5  # показываем 5 последних диалогов (по updated_at DESC)

STATE_RENAME = 1

CB_OPEN = "dlg:open"
CB_RENAME = "dlg:rename"
CB_DELETE = "dlg:delete"
CB_DELETE_OK = "dlg:delete_ok"
CB_CANCEL = "dlg:cancel"
CB_NEW = "dlg:new"
CB_REFRESH = "dlg:refresh"
CB_CLOSE = "dlg:close"


# ---------------------------
# Вспомогательные функции
# ---------------------------


def _parse_cb(data: str) -> Tuple[str, Optional[int]]:
    # пример: dlg:open:59
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
    try:
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return "-"


def _best_dt(d):
    # created_at приоритетнее; если по историческим данным NULL, используем updated_at
    return getattr(d, "created_at", None) or getattr(d, "updated_at", None)


def _date_prefix(d) -> str:
    dt = _best_dt(d)
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def _truncate(text: str, max_len: int = 60) -> str:
    text = (text or "").strip()
    if not text:
        return "Диалог"
    return text if len(text) <= max_len else (text[: max_len - 1] + "…")


def _display_name(d) -> str:
    """Отображаемое имя: YYYY-MM-DD_<title> (без дублирования префикса)."""
    prefix = _date_prefix(d)
    title = (getattr(d, "title", "") or "").strip()

    if title:
        # если уже начинается с YYYY-MM-DD_ — не дублируем
        if len(title) >= 11 and title[:10] == prefix and title[10:11] == "_":
            return _truncate(title, 80)
        return f"{prefix}_{_truncate(title, 70)}"

    return f"{prefix}_Диалог"


def _build_keyboard(dialogs, active_id: Optional[int]) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    for d in dialogs:
        is_active = bool(active_id and d.id == active_id)

        # Кнопка выбора — компактная (имена выводятся в тексте сообщения, так проще выравнивать слева)
        kb.append([
            InlineKeyboardButton(
                text=("✅ Активный" if is_active else "Выбрать") + f" #{d.id}",
                callback_data=f"{CB_OPEN}:{d.id}",
            )
        ])

        # Кнопки действий
        kb.append([
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"{CB_RENAME}:{d.id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"{CB_DELETE}:{d.id}"),
        ])

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
        elif update.callback_query:
            await update.callback_query.message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")

    if not ds or not repo or not update.effective_user:
        if update.message:
            await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return

    # internal user_id
    u = repo.ensure_user(str(update.effective_user.id))
    dialogs = repo.list_dialogs(u.id, limit=SHOW_LIMIT)

    if not dialogs:
        text = "Диалогов пока нет. Нажмите «➕ Новый» (или используйте /reset)."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый", callback_data=f"{CB_NEW}:0")]])
        if update.message:
            await update.message.reply_text(text, reply_markup=kb)
        else:
            await update.callback_query.message.edit_text(text, reply_markup=kb)
        return

    active = repo.get_active_dialog(u.id)
    active_id = active.id if active else None

    # ВАЖНО: имена выводим в тексте сообщения, чтобы визуально выровнять по левому краю.
    lines: List[str] = ["*Диалоги (последние 5)*"]
    lines.append(f"Активный: *{active_id}*" if active_id else "Активный: _не выбран_")
    lines.append("")

    for d in dialogs:
        mark = "✅" if active_id and d.id == active_id else "•"
        name = _display_name(d)
        created_s = _fmt_dt(getattr(d, "created_at", None) or getattr(d, "updated_at", None))
        updated_s = _fmt_dt(getattr(d, "updated_at", None) or getattr(d, "created_at", None))
        lines.append(f"{mark} *{d.id}* — {name}")
        lines.append(f"   _создан:_ `{created_s}`   _изм.:_ `{updated_s}`")

    text = "\n".join(lines)
    kb = _build_keyboard(dialogs, active_id)

    if update.callback_query and edit:
        await update.callback_query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


# ---------------------------
# Команды и callbacks
# ---------------------------


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _render(update, context, edit=False)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оставляем как технический алиас на создание нового диалога."""
    az: AuthzService = context.bot_data.get("svc_authz")
    if az and update.effective_user and not az.is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещен.")
        return

    ds: DialogService = context.bot_data.get("svc_dialog")
    if not ds or not update.effective_user:
        await update.message.reply_text("⚠️ Сервис диалогов не настроен.")
        return

    d = ds.new_dialog(update.effective_user.id, title="")
    await update.message.reply_text(f"Создан новый диалог: {d.id}")


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

    if action == CB_CLOSE:
        await q.message.edit_reply_markup(reply_markup=None)
        return

    if action == CB_REFRESH:
        await _render(update, context, edit=True)
        return

    if action == CB_NEW:
        ds.new_dialog(update.effective_user.id, title="")
        await _render(update, context, edit=True)
        return

    if action == CB_CANCEL:
        context.user_data.pop("rename_dialog_id", None)
        await _render(update, context, edit=True)
        return ConversationHandler.END

    if did is None:
        await _render(update, context, edit=True)
        return

    # Ownership check
    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(did, u.id)
    if not d:
        await q.message.reply_text("⛔ Диалог не найден или недоступен.")
        return

    if action == CB_OPEN:
        ok = ds.switch_dialog(update.effective_user.id, did)
        if ok:
            await q.message.reply_text(f"⭐ Активный диалог: {did}")
        else:
            await q.message.reply_text("⛔ Не удалось выбрать диалог.")
        await _render(update, context, edit=True)
        return

    if action == CB_DELETE:
        name = _display_name(d)
        confirm = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Удалить", callback_data=f"{CB_DELETE_OK}:{did}"),
                InlineKeyboardButton("↩️ Отмена", callback_data=f"{CB_CANCEL}:0"),
            ]
        ])
        await q.message.reply_text(
            f"Удалить диалог *{did}*?\n_{name}_",
            reply_markup=confirm,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == CB_DELETE_OK:
        repo.delete_dialog(did)
        await q.message.reply_text("🗑 Диалог удалён.")
        await _render(update, context, edit=True)
        return

    if action == CB_RENAME:
        context.user_data["rename_dialog_id"] = did
        prefix = _date_prefix(d)
        await q.message.reply_text(
            "Введите новое имя диалога.\n"
            f"Отображение: `{prefix}_<имя>`\n"
            "Можно отправить пустое сообщение, чтобы очистить пользовательскую часть.",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN,
        )
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

    # Ownership check
    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(did), u.id)
    if not d:
        await update.message.reply_text("⛔ Диалог не найден или недоступен.")
        context.user_data.pop("rename_dialog_id", None)
        return ConversationHandler.END

    # Храним пользовательскую часть; UI автоматически добавит YYYY-MM-DD_ при отображении.
    repo.rename_dialog(int(did), raw)
    context.user_data.pop("rename_dialog_id", None)

    await update.message.reply_text("✏️ Название обновлено.")
    await _render(update, context, edit=False)
    return ConversationHandler.END


def register(app: Application) -> None:
    # ЕДИНАЯ точка управления диалогами
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))

    # /reset оставляем как совместимость (можно скрыть из set_my_commands)
    app.add_handler(CommandHandler("reset", cmd_reset))

    # Callback-управление меню /dialogs
    app.add_handler(
        CallbackQueryHandler(
            cb_dialogs,
            pattern=r"^dlg:(open|rename|delete|delete_ok|cancel|new|refresh|close):",
        )
    )

    rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_dialogs, pattern=r"^dlg:rename:\d+$")],
        states={STATE_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rename_receive)]},
        fallbacks=[CallbackQueryHandler(cb_dialogs, pattern=r"^dlg:cancel:0$")],
        name="dialogs_rename",
        persistent=False,
    )
    app.add_handler(rename_conv)
