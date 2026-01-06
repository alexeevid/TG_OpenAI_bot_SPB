# app/handlers/dialogs.py
from __future__ import annotations

import re
from datetime import datetime
from html import escape
from math import ceil
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from app.db.repo_dialogs import DialogsRepo

# --- callbacks ---
BTN_NEW = "dlg:new"
BTN_PAGE_PREV = "dlg:prev"
BTN_PAGE_NEXT = "dlg:next"
BTN_OPEN_PREFIX = "dlg:open:"
BTN_RENAME_PREFIX = "dlg:rename:"
BTN_DELETE_PREFIX = "dlg:delete:"
NOOP = "noop"

# --- UI constants ---
PAGE_SIZE = 5  # требование: максимум 5 диалогов на странице
TITLE_MAX = 64

# Принимаем разные варианты дат, но нормализуем в YYYY-MM-DD
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(.+)$", re.UNICODE)


def _fmt_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    # Коротко и стабильно. Если хочешь время — поменяем.
    return dt.strftime("%Y-%m-%d")


def _mask_title(created_at: Optional[datetime], name: str) -> str:
    """
    Всегда приводит к маске: YYYY-MM-DD_<name>
    """
    base = (name or "").strip()
    if not base:
        base = "Новый диалог"

    # Если пользователь вставил уже с датой — забираем только "имя"
    m = DATE_PREFIX_RE.match(base)
    if m:
        base = (m.group(1) or "").strip() or "Новый диалог"

    date_part = _fmt_date(created_at)
    return f"{date_part}_{base}"


def _display_title(d, *, is_active: bool) -> str:
    """
    Одна строка: [✅] YYYY-MM-DD_Имя
    """
    title = (getattr(d, "title", None) or "").strip()
    if not title:
        title = _mask_title(getattr(d, "created_at", None), "")

    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1] + "…"

    prefix = "✅ " if is_active else ""
    return prefix + title


def _build_keyboard(dialogs, *, active_dialog_id: Optional[int], page: int, pages_total: int) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []

    # Для каждого диалога — 2 строки:
    # 1) название одной кнопкой
    # 2) "🕒 дата" + ✏️ + 🗑 в одной строке
    for d in dialogs:
        did = int(getattr(d, "id", 0))
        is_active = active_dialog_id == did

        title_btn = InlineKeyboardButton(
            _display_title(d, is_active=is_active),
            callback_data=f"{BTN_OPEN_PREFIX}{did}",
        )
        kb.append([title_btn])

        updated = _fmt_date(getattr(d, "updated_at", None))
        info_btn = InlineKeyboardButton(f"🕒 {updated}", callback_data=NOOP)
        edit_btn = InlineKeyboardButton("✏️", callback_data=f"{BTN_RENAME_PREFIX}{did}")
        del_btn = InlineKeyboardButton("🗑", callback_data=f"{BTN_DELETE_PREFIX}{did}")
        kb.append([info_btn, edit_btn, del_btn])

    # Навигация
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=BTN_PAGE_PREV))
    nav.append(InlineKeyboardButton(f"{page}/{pages_total}", callback_data=NOOP))
    if page < pages_total:
        nav.append(InlineKeyboardButton("➡️", callback_data=BTN_PAGE_NEXT))
    kb.append(nav)

    # Отдельная кнопка "Новый диалог"
    kb.append([InlineKeyboardButton("➕ Новый диалог", callback_data=BTN_NEW)])

    return InlineKeyboardMarkup(kb)


async def _render(update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
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

    text = (
        "📚 <b>Диалоги</b>\n"
        "Название всегда в формате <b>YYYY-MM-DD_Имя</b>.\n"
        "Нажмите на диалог, чтобы сделать его активным.\n"
        "<i>🕒 — последняя дата изменения, ✏️ — переименовать, 🗑 — удалить</i>"
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


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /rename <id> <new name>
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user or not update.effective_message:
        return

    parts = (update.effective_message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.effective_message.reply_text("Использование: /rename <dialog_id> <новое имя>")
        return

    dialog_id = int(parts[1])
    new_name = parts[2].strip()

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(dialog_id, u.id)
    if not d:
        await update.effective_message.reply_text("Диалог не найден.")
        return

    masked = _mask_title(getattr(d, "created_at", None), new_name)
    repo.rename_dialog(d.id, masked)
    await update.effective_message.reply_text("✅ Переименовано.")
    await _render(update, context, edit=False)


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
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
        # Принудительно присваиваем маску по created_at
        masked = _mask_title(getattr(d, "created_at", None), "Новый диалог")
        repo.rename_dialog(d.id, masked)
        repo.set_active_dialog(u.id, d.id)

        await update.callback_query.answer("Создан новый диалог")
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_OPEN_PREFIX):
        dialog_id = int(data[len(BTN_OPEN_PREFIX) :])
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            await update.callback_query.answer("Диалог не найден", show_alert=True)
            return
        repo.set_active_dialog(u.id, d.id)
        await update.callback_query.answer("Активный диалог обновлён")
        await _render(update, context, edit=True)
        return

    if data.startswith(BTN_RENAME_PREFIX):
        dialog_id = int(data[len(BTN_RENAME_PREFIX) :])
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            await update.callback_query.answer("Диалог не найден", show_alert=True)
            return

        context.user_data["dlg_rename_id"] = dialog_id
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "Введите новое <b>имя</b> диалога одним сообщением.\n"
            "Дата будет сохранена автоматически как <b>YYYY-MM-DD_Имя</b>.",
            parse_mode="HTML",
        )
        return

    if data.startswith(BTN_DELETE_PREFIX):
        dialog_id = int(data[len(BTN_DELETE_PREFIX) :])
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            await update.callback_query.answer("Диалог не найден", show_alert=True)
            return

        repo.delete_dialog(dialog_id)
        await update.callback_query.answer("Удалено")
        await _render(update, context, edit=True)
        return

    await update.callback_query.answer()


async def on_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ловит следующий текст после нажатия ✏️ и переименовывает диалог.
    """
    repo: DialogsRepo = context.bot_data.get("repo_dialogs")
    if not repo or not update.effective_user or not update.effective_message:
        return

    dialog_id = context.user_data.get("dlg_rename_id")
    if not dialog_id:
        return  # это не rename-сценарий

    new_name = (update.effective_message.text or "").strip()
    if not new_name:
        await update.effective_message.reply_text("Имя не должно быть пустым. Попробуйте ещё раз.")
        return

    u = repo.ensure_user(str(update.effective_user.id))
    d = repo.get_dialog_for_user(int(dialog_id), u.id)
    if not d:
        context.user_data.pop("dlg_rename_id", None)
        await update.effective_message.reply_text("Диалог не найден.")
        return

    masked = _mask_title(getattr(d, "created_at", None), new_name)
    repo.rename_dialog(d.id, masked)

    context.user_data.pop("dlg_rename_id", None)
    await update.effective_message.reply_text("✅ Переименовано.")
    await _render(update, context, edit=False)


def build_handlers() -> List:
    return [
        CommandHandler("dialogs", cmd_dialogs),
        CallbackQueryHandler(on_cb, pattern=r"^(dlg:.*|noop)$"),
        CommandHandler("rename", cmd_rename),
        # Текст после ✏️ — без команды
        # Важно: не перехватывает обычный чат, т.к. срабатывает только при dlg_rename_id в user_data.
        CommandHandler("__dialogs_rename_text__", on_rename_text),  # заглушка (ниже регистрируем MessageHandler)
    ]


def register(app) -> None:
    """
    Совместимость с main.py: dialogs.register(app)
    """
    from telegram.ext import MessageHandler, filters

    app.add_handler(CommandHandler("dialogs", cmd_dialogs))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^(dlg:.*|noop)$"))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_rename_text), group=10)
