from __future__ import annotations

import re
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..services.authz_service import AuthzService
from ..db.repo_access import AccessRepo

# Conversation states
MENU, WAIT_ALLOW_ID = range(2)

CB_NS = "accui"  # namespace для callback_data


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    az: AuthzService | None = context.bot_data.get("svc_authz")
    uid = update.effective_user.id if update.effective_user else None
    return bool(az and uid is not None and az.is_admin(uid))


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Optional[AccessRepo]:
    return context.bot_data.get("repo_access")


def _kbd_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить пользователя", callback_data=f"{CB_NS}:allow")],
            [InlineKeyboardButton("📋 Список (кратко)", callback_data=f"{CB_NS}:list")],
            [InlineKeyboardButton("✖ Закрыть", callback_data=f"{CB_NS}:close")],
        ]
    )


def _kbd_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩ Отмена", callback_data=f"{CB_NS}:back")]]
    )


def _extract_tg_id_from_message(update: Update, text: str) -> Optional[int]:
    """
    Приоритет:
    1) если админ отвечает на сообщение пользователя — берём reply_to_message.from_user.id
    2) иначе пробуем извлечь число из текста
    """
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        try:
            return int(msg.reply_to_message.from_user.id)
        except Exception:
            pass

    t = (text or "").strip()
    if not t:
        return None

    # допускаем "id: 123" или просто "123"
    m = re.search(r"(\d{5,})", t)
    if not m:
        return None

    try:
        return int(m.group(1))
    except Exception:
        return None


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен (проверь main.py).")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "🔐 Управление доступами (inline)\nВыбери действие:",
        reply_markup=_kbd_menu(),
    )
    return MENU


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END

    await q.answer()  # важно, иначе будет “крутилка” у клиента

    if not _is_admin(update, context):
        try:
            await q.edit_message_text("⛔ Доступ запрещен.")
        except Exception:
            pass
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        try:
            await q.edit_message_text("⚠️ repo_access не подключен (проверь main.py).")
        except Exception:
            pass
        return ConversationHandler.END

    data = q.data or ""
    _, action = data.split(":", 1) if ":" in data else ("", "")

    if action == "allow":
        # просим tg_id или ответом на сообщение пользователя
        try:
            await q.edit_message_text(
                "Отправь tg_id пользователя (числом) или ответь на сообщение пользователя и пришли любой текст.\n\n"
                "Пример: `123456789`",
                parse_mode="Markdown",
                reply_markup=_kbd_cancel(),
            )
        except Exception:
            await q.message.reply_text(
                "Отправь tg_id пользователя (числом) или ответь на сообщение пользователя и пришли любой текст.\n\n"
                "Пример: `123456789`",
                parse_mode="Markdown",
                reply_markup=_kbd_cancel(),
            )
        return WAIT_ALLOW_ID

    if action == "list":
        rows = repo.list(limit=50)
        if not rows:
            text = "📋 Список пуст. (DB-режим включится, когда появится хотя бы 1 запись)"
        else:
            lines = ["📋 Доступы (первые 50):"]
            for r in rows:
                flags = []
                flags.append("✅" if r.is_allowed else "⛔")
                if r.is_admin:
                    flags.append("👑")
                note = f" — {r.note}" if r.note else ""
                lines.append(f"• {r.tg_id} {' '.join(flags)}{note}")
            text = "\n".join(lines)

        try:
            await q.edit_message_text(text, reply_markup=_kbd_menu())
        except Exception:
            await q.message.reply_text(text, reply_markup=_kbd_menu())
        return MENU

    if action == "close":
        try:
            await q.edit_message_text("Ок, закрыто.")
        except Exception:
            pass
        return ConversationHandler.END

    if action == "back":
        try:
            await q.edit_message_text("🔐 Управление доступами (inline)\nВыбери действие:", reply_markup=_kbd_menu())
        except Exception:
            await q.message.reply_text("🔐 Управление доступами (inline)\nВыбери действие:", reply_markup=_kbd_menu())
        return MENU

    return MENU


async def on_allow_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен (проверь main.py).")
        return ConversationHandler.END

    msg = update.effective_message
    if not msg:
        return ConversationHandler.END

    # покажем “typing…”
    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    tg_id = _extract_tg_id_from_message(update, msg.text or "")
    if tg_id is None:
        await msg.reply_text(
            "⚠️ Не смог распознать tg_id.\n"
            "Отправь число (tg_id) или ответь на сообщение пользователя и пришли любой текст.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_ALLOW_ID

    repo.upsert(tg_id, allow=True, admin=False, note="added via inline")
    await msg.reply_text(f"✅ Пользователь добавлен (allow): {tg_id}", reply_markup=_kbd_menu())
    return MENU


def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("users", cmd_users)],
        states={
            MENU: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
            ],
            WAIT_ALLOW_ID: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_allow_id),
            ],
        },
        fallbacks=[CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:")],
        name="access_ui",
        persistent=False,
    )

    # Регистрируем раньше общего text handler (у вас text.py в group=10),
    # ConversationHandler по умолчанию живёт в группе 0 — этого достаточно.
    app.add_handler(conv)
