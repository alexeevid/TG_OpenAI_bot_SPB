from __future__ import annotations

import re
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..db.repo_access import AccessRepo
from ..services.authz_service import AuthzService

# Conversation states
MENU, WAIT_ALLOW_MASS, WAIT_BLOCK_MASS, WAIT_DELETE_MASS, WAIT_ADMIN_ONE, WAIT_UNADMIN_ONE = range(6)

CB_NS = "acc"  # namespace для callback_data


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Optional[AccessRepo]:
    return context.bot_data.get("repo_access")


def _az(context: ContextTypes.DEFAULT_TYPE) -> Optional[AuthzService]:
    return context.bot_data.get("svc_authz")


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    az = _az(context)
    if not az:
        return False
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return False
    return az.is_admin(uid)


def _mk_cb(action: str) -> str:
    return f"{CB_NS}:{action}"


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Разрешить (массово)", callback_data=_mk_cb("allow_mass"))],
            [InlineKeyboardButton("⛔ Заблокировать (массово)", callback_data=_mk_cb("block_mass"))],
            [InlineKeyboardButton("🗑 Удалить записи (массово)", callback_data=_mk_cb("delete_mass"))],
            [InlineKeyboardButton("👑 Сделать админом (по одному)", callback_data=_mk_cb("admin_one"))],
            [InlineKeyboardButton("🙅 Убрать админа (по одному)", callback_data=_mk_cb("unadmin_one"))],
            [InlineKeyboardButton("📋 Показать список", callback_data=_mk_cb("list"))],
            [InlineKeyboardButton("⬅️ Закрыть", callback_data=_mk_cb("close"))],
        ]
    )


def _parse_ids(text: str, update: Update) -> List[int]:
    """
    Парсит tg_id из текста:
    - любые числа 5+ символов
    - поддержка 'reply' на сообщение пользователя (берет его id)
    """
    ids: List[int] = []
    text = (text or "").strip()

    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        try:
            ids.append(int(msg.reply_to_message.from_user.id))
        except Exception:
            pass

    for m in re.findall(r"\d{5,}", text or ""):
        try:
            ids.append(int(m))
        except Exception:
            pass

    seen = set()
    out: List[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


async def _reply(update: Update, text: str, context: ContextTypes.DEFAULT_TYPE, kb: InlineKeyboardMarkup | None = None):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    if update.callback_query:
        # отвечаем через edit, чтобы меню было "живым"
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
            return
        except Exception:
            # fallback
            pass

    await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else None
    az = _az(context)
    is_admin = az.is_admin(uid) if (az and uid is not None) else False

    # debug-строка, чтобы по логам сразу видеть, почему не пускает
    await update.effective_message.reply_text(
        f"DEBUG /access: uid={uid}, has_authz={bool(az)}, is_admin={is_admin}"
    )

    if not is_admin:
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "Меню управления доступами:", reply_markup=_menu_kb()
    )
    return MENU


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END

    try:
        await q.answer()
    except Exception:
        pass

    if not _is_admin(update, context):
        await _reply(update, "⛔ Доступ запрещен.", context)
        return ConversationHandler.END

    data = q.data or ""
    if not data.startswith(f"{CB_NS}:"):
        return MENU

    action = data.split(":", 1)[1].strip()

    if action == "close":
        try:
            await q.edit_message_text("Закрыто.")
        except Exception:
            pass
        return ConversationHandler.END

    if action == "list":
        repo = _repo(context)
        if not repo:
            await _reply(update, "❌ repo_access не подключен в bot_data.", context, kb=_menu_kb())
            return MENU

        rows = repo.list_all()
        if not rows:
            await _reply(update, "Список пуст.", context, kb=_menu_kb())
            return MENU

        # ожидаем, что repo возвращает объекты/словарики с tg_id / is_blocked / is_admin / note
        lines = []
        for r in rows:
            tg_id = getattr(r, "tg_id", None) if not isinstance(r, dict) else r.get("tg_id")
            is_blocked = getattr(r, "is_blocked", None) if not isinstance(r, dict) else r.get("is_blocked")
            is_admin = getattr(r, "is_admin", None) if not isinstance(r, dict) else r.get("is_admin")
            note = getattr(r, "note", None) if not isinstance(r, dict) else r.get("note")
            flags = []
            if is_admin:
                flags.append("admin")
            if is_blocked:
                flags.append("blocked")
            fl = f" ({', '.join(flags)})" if flags else ""
            note_part = f" — {note}" if note else ""
            lines.append(f"- {tg_id}{fl}{note_part}")

        text = "📋 Список доступов:\n" + "\n".join(lines)
        await _reply(update, text, context, kb=_menu_kb())
        return MENU

    if action == "allow_mass":
        await _reply(update, "Пришли tg_id (можно списком/через пробел). Также можно ответить (reply) на сообщение пользователя.", context)
        return WAIT_ALLOW_MASS

    if action == "block_mass":
        await _reply(update, "Пришли tg_id для блокировки (можно списком). Также можно reply на сообщение пользователя.", context)
        return WAIT_BLOCK_MASS

    if action == "delete_mass":
        await _reply(update, "Пришли tg_id для удаления записей (можно списком).", context)
        return WAIT_DELETE_MASS

    if action == "admin_one":
        await _reply(update, "Пришли tg_id пользователя, которого сделать админом (или reply).", context)
        return WAIT_ADMIN_ONE

    if action == "unadmin_one":
        await _reply(update, "Пришли tg_id админа, у которого снять админство (или reply).", context)
        return WAIT_UNADMIN_ONE

    await _reply(update, "Неизвестная команда меню.", context, kb=_menu_kb())
    return MENU


async def on_allow_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("❌ repo_access не подключен в bot_data.")
        return ConversationHandler.END

    ids = _parse_ids(update.effective_message.text or "", update)
    if not ids:
        await update.effective_message.reply_text("Не нашёл tg_id. Пришли числа (5+ цифр) или сделай reply на сообщение пользователя.")
        return WAIT_ALLOW_MASS

    ok = 0
    for tg_id in ids:
        try:
            repo.allow(tg_id)
            ok += 1
        except Exception:
            pass

    await update.effective_message.reply_text(f"✅ Разрешено: {ok}/{len(ids)}", reply_markup=_menu_kb())
    return MENU


async def on_block_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("❌ repo_access не подключен в bot_data.")
        return ConversationHandler.END

    ids = _parse_ids(update.effective_message.text or "", update)
    if not ids:
        await update.effective_message.reply_text("Не нашёл tg_id. Пришли числа (5+ цифр) или сделай reply на сообщение пользователя.")
        return WAIT_BLOCK_MASS

    ok = 0
    for tg_id in ids:
        try:
            repo.block(tg_id)
            ok += 1
        except Exception:
            pass

    await update.effective_message.reply_text(f"⛔ Заблокировано: {ok}/{len(ids)}", reply_markup=_menu_kb())
    return MENU


async def on_delete_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("❌ repo_access не подключен в bot_data.")
        return ConversationHandler.END

    ids = _parse_ids(update.effective_message.text or "", update)
    if not ids:
        await update.effective_message.reply_text("Не нашёл tg_id. Пришли числа (5+ цифр).")
        return WAIT_DELETE_MASS

    ok = 0
    for tg_id in ids:
        try:
            repo.delete(tg_id)
            ok += 1
        except Exception:
            pass

    await update.effective_message.reply_text(f"🗑 Удалено: {ok}/{len(ids)}", reply_markup=_menu_kb())
    return MENU


async def on_admin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("❌ repo_access не подключен в bot_data.")
        return ConversationHandler.END

    ids = _parse_ids(update.effective_message.text or "", update)
    if not ids:
        await update.effective_message.reply_text("Не нашёл tg_id. Пришли число (5+ цифр) или reply на сообщение пользователя.")
        return WAIT_ADMIN_ONE

    tg_id = ids[0]
    try:
        repo.make_admin(tg_id)
        await update.effective_message.reply_text(f"👑 Сделал админом: {tg_id}", reply_markup=_menu_kb())
    except Exception:
        await update.effective_message.reply_text(f"❌ Не смог сделать админом: {tg_id}", reply_markup=_menu_kb())

    return MENU


async def on_unadmin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("❌ repo_access не подключен в bot_data.")
        return ConversationHandler.END

    ids = _parse_ids(update.effective_message.text or "", update)
    if not ids:
        await update.effective_message.reply_text("Не нашёл tg_id. Пришли число (5+ цифр) или reply на сообщение пользователя.")
        return WAIT_UNADMIN_ONE

    tg_id = ids[0]
    try:
        repo.unmake_admin(tg_id)
        await update.effective_message.reply_text(f"🙅 Снял админство: {tg_id}", reply_markup=_menu_kb())
    except Exception:
        await update.effective_message.reply_text(f"❌ Не смог снять админство: {tg_id}", reply_markup=_menu_kb())

    return MENU


def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("access", cmd_access)],
        states={
            MENU: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
            ],
            WAIT_ALLOW_MASS: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_allow_mass),
            ],
            WAIT_BLOCK_MASS: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_block_mass),
            ],
            WAIT_DELETE_MASS: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_delete_mass),
            ],
            WAIT_ADMIN_ONE: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_one),
            ],
            WAIT_UNADMIN_ONE: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{CB_NS}:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_unadmin_one),
            ],
        },
        fallbacks=[],
        name="access",
        persistent=False,
        per_user=True,
        per_chat=True,
        per_message=False,  # важно для меню, которое отправляется отдельным сообщением (inline-кнопки)
        allow_reentry=True,
    )
    app.add_handler(conv)
