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
from ..settings import get_settings

# Conversation states
MENU, WAIT_ALLOW_MASS, WAIT_BLOCK_MASS, WAIT_DELETE_MASS, WAIT_ADMIN_ONE, WAIT_UNADMIN_ONE = range(6)

CB_NS = "acc"  # namespace для callback_data

# --- callback patterns (строго, чтобы не перехватывать лишнее) ---
CB_MENU_PATTERN = rf"^{CB_NS}:(allow_mass|block_mass|delete_mass|admin_one|unadmin_one|list|cancel|close)$"
CB_NAV_PATTERN = rf"^{CB_NS}:(list|cancel|close)$"


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Optional[AccessRepo]:
    return context.bot_data.get("repo_access")


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    az = context.bot_data.get("svc_authz")
    uid = update.effective_user.id if update.effective_user else None
    return bool(az and uid is not None and az.is_admin(uid))


def _kbd_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Добавить (массово)", callback_data=f"{CB_NS}:allow_mass"),
                InlineKeyboardButton("⛔ Заблокировать (массово)", callback_data=f"{CB_NS}:block_mass"),
            ],
            [
                InlineKeyboardButton("👑 Назначить админом", callback_data=f"{CB_NS}:admin_one"),
                InlineKeyboardButton("⬇️ Снять админа", callback_data=f"{CB_NS}:unadmin_one"),
            ],
            [
                InlineKeyboardButton("🗑 Удалить (массово)", callback_data=f"{CB_NS}:delete_mass"),
            ],
            [
                InlineKeyboardButton("📋 Список", callback_data=f"{CB_NS}:list"),
                InlineKeyboardButton("❌ Закрыть", callback_data=f"{CB_NS}:close"),
            ],
        ]
    )


def _kbd_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_NS}:cancel")]]
    )


def _parse_target_id(update: Update, args: List[str]) -> Optional[int]:
    """Берёт tg_id из аргументов или из reply."""
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        try:
            return int(msg.reply_to_message.from_user.id)
        except Exception:
            pass

    for a in args:
        m = re.search(r"\d{5,}", a or "")
        if m:
            try:
                return int(m.group(0))
            except Exception:
                continue
    return None


def _extract_ids_from_text(update: Update, text: str) -> List[int]:
    """
    Вынимает tg_id из текста:
    - любые числа 5+ цифр
    - если это reply на сообщение пользователя — добавит id автора
    """
    ids: List[int] = []

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

    # uniq, сохранить порядок
    seen = set()
    out: List[int] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _format_list(repo: AccessRepo) -> str:
    try:
        rows = repo.list(limit=200)
    except Exception:
        rows = []

    try:
        db_mode = repo.has_any_entries()
    except Exception:
        db_mode = False

    header = "📋 Доступы (DB-режим: включён ✅)" if db_mode else "📋 Доступы (DB-режим: выключен ⛔ — таблица пуста)"
    if not rows:
        return header + "\n\n(пусто)"

    lines = [header, ""]
    for r in rows:
        flags = []
        flags.append("✅allow" if r.is_allowed else "⛔block")
        if r.is_admin:
            flags.append("👑admin")
        note = f" — {r.note}" if getattr(r, "note", "") else ""
        lines.append(f"• {r.tg_id}: {' '.join(flags)}{note}")
    return "\n".join(lines)


async def _send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_chat:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /access
    /access list
    /access allow|block|admin|unadmin|delete <tgid> [note]
    или reply на сообщение пользователя + /access allow
    """
    await _send_typing(update, context)

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен (проверь main.py).")
        return ConversationHandler.END

    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    # --- CLI режим ---
    args = context.args or []
    if args:
        sub = args[0].lower().strip()

        if sub == "list":
            await update.effective_message.reply_text(_format_list(repo))
            return ConversationHandler.END

        if sub in {"allow", "block", "admin", "unadmin", "delete"}:
            target = _parse_target_id(update, args[1:])
            if not target:
                await update.effective_message.reply_text(
                    "⚠️ Не вижу tg_id.\n"
                    "Пример:\n"
                    "• /access allow 123456789\n"
                    "или\n"
                    "• ответь на сообщение пользователя и выполни /access allow"
                )
                return ConversationHandler.END

            note = " ".join(args[2:]).strip() if len(args) > 2 else ""

            if sub == "allow":
                repo.upsert(target, allow=True, admin=False, note=note)
                await update.effective_message.reply_text(f"✅ Доступ разрешён: {target}")
                return ConversationHandler.END

            if sub == "block":
                repo.upsert(target, allow=False, admin=False, note=note)
                await update.effective_message.reply_text(f"⛔ Доступ запрещён: {target}")
                return ConversationHandler.END

            if sub == "admin":
                repo.upsert(target, allow=True, admin=True, note=note)
                await update.effective_message.reply_text(f"👑 Назначен админ: {target}")
                return ConversationHandler.END

            if sub == "unadmin":
                repo.upsert(target, allow=True, admin=False, note=note)
                await update.effective_message.reply_text(f"⬇️ Снят админ: {target}")
                return ConversationHandler.END

            if sub == "delete":
                ok = repo.delete(target)
                await update.effective_message.reply_text(("🗑 Удалено: " if ok else "⚠️ Не найдено: ") + str(target))
                return ConversationHandler.END

        await update.effective_message.reply_text(
            "⚠️ Неизвестная команда.\n"
            "Доступные:\n"
            "• /access\n"
            "• /access list\n"
            "• /access allow|block|admin|unadmin|delete <tgid> [note]"
        )
        return ConversationHandler.END

    # --- UI режим ---
    await update.effective_message.reply_text("🔐 Управление доступами", reply_markup=_kbd_menu())
    return MENU


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return ConversationHandler.END

    await q.answer()

    if not _is_admin(update, context):
        try:
            await q.edit_message_text("⛔ Доступ запрещен.", reply_markup=None)
        except Exception:
            pass
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        try:
            await q.edit_message_text("⚠️ repo_access не подключен (проверь main.py).", reply_markup=None)
        except Exception:
            pass
        return ConversationHandler.END

    data = q.data or ""
    if not re.match(CB_MENU_PATTERN, data):
        return MENU

    action = data.split(":", 1)[1].strip()

    if action == "allow_mass":
        await q.edit_message_text(
            "➕ Добавление (массово)\n"
            "Пришли tg_id (можно списком/через пробел/строки).\n"
            "Можно также reply на сообщение пользователя.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_ALLOW_MASS

    if action == "block_mass":
        await q.edit_message_text(
            "⛔ Блокировка (массово)\n"
            "Пришли tg_id (можно списком/через пробел/строки).\n"
            "Можно также reply на сообщение пользователя.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_BLOCK_MASS

    if action == "delete_mass":
        await q.edit_message_text(
            "🗑 Удаление (массово)\n"
            "Пришли tg_id (можно списком/через пробел/строки).\n"
            "Можно также reply на сообщение пользователя.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_DELETE_MASS

    if action == "admin_one":
        await q.edit_message_text(
            "👑 Назначить админом\n"
            "Пришли tg_id (один) или ответь reply на сообщение пользователя.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_ADMIN_ONE

    if action == "unadmin_one":
        await q.edit_message_text(
            "⬇️ Снять админа\n"
            "Пришли tg_id (один) или ответь reply на сообщение пользователя.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_UNADMIN_ONE

    if action == "list":
        await q.edit_message_text(_format_list(repo), reply_markup=_kbd_menu())
        return MENU

    if action == "cancel":
        await q.edit_message_text("🔐 Управление доступами", reply_markup=_kbd_menu())
        return MENU

    if action == "close":
        await q.edit_message_text("Ок, закрыто.", reply_markup=None)
        return ConversationHandler.END

    return MENU


async def _apply_mass(update: Update, context: ContextTypes.DEFAULT_TYPE, *, allow: Optional[bool], delete: bool) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен (проверь main.py).")
        return ConversationHandler.END

    text = update.effective_message.text or ""
    ids = _extract_ids_from_text(update, text)

    if not ids:
        await update.effective_message.reply_text("⚠️ Не вижу tg_id. Пришли числа (5+ цифр) или ответь reply.")
        return MENU

    ok = 0
    for tg_id in ids:
        try:
            if delete:
                if repo.delete(tg_id):
                    ok += 1
            else:
                repo.upsert(tg_id, allow=bool(allow), admin=False, note="ui mass")
                ok += 1
        except Exception:
            pass

    if delete:
        await update.effective_message.reply_text(f"🗑 Удалено: {ok}/{len(ids)}", reply_markup=_kbd_menu())
    else:
        if allow:
            await update.effective_message.reply_text(f"✅ Разрешено: {ok}/{len(ids)}", reply_markup=_kbd_menu())
        else:
            await update.effective_message.reply_text(f"⛔ Заблокировано: {ok}/{len(ids)}", reply_markup=_kbd_menu())

    return MENU


async def _apply_one(update: Update, context: ContextTypes.DEFAULT_TYPE, *, admin: bool) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен (проверь main.py).")
        return ConversationHandler.END

    target = _parse_target_id(update, [update.effective_message.text or ""])
    if not target:
        await update.effective_message.reply_text("⚠️ Не вижу tg_id. Пришли число (5+ цифр) или ответь reply.")
        return MENU

    try:
        repo.upsert(target, allow=True, admin=admin, note="ui one")
        await update.effective_message.reply_text(
            ("👑 Назначен админ: " if admin else "⬇️ Снят админ: ") + str(target),
            reply_markup=_kbd_menu(),
        )
    except Exception:
        await update.effective_message.reply_text("⚠️ Не удалось выполнить операцию.", reply_markup=_kbd_menu())

    return MENU


async def on_allow_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_typing(update, context)
    return await _apply_mass(update, context, allow=True, delete=False)


async def on_block_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_typing(update, context)
    return await _apply_mass(update, context, allow=False, delete=False)


async def on_delete_mass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_typing(update, context)
    return await _apply_mass(update, context, allow=None, delete=True)


async def on_admin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_typing(update, context)
    return await _apply_one(update, context, admin=True)


async def on_unadmin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _send_typing(update, context)
    return await _apply_one(update, context, admin=False)


async def on_nav_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Во время ожидания текста всё равно должны работать назад/список/закрыть."""
    q = update.callback_query
    if not q:
        return ConversationHandler.END
    data = q.data or ""
    if not re.match(CB_NAV_PATTERN, data):
        # не перехватываем чужие callback'и
        return ConversationHandler.END
    return await on_menu_click(update, context)


def register(app: Application) -> None:
    settings = get_settings()

    conv = ConversationHandler(
        entry_points=[CommandHandler("access", cmd_access)],
        states={
            MENU: [
                CallbackQueryHandler(on_menu_click, pattern=CB_MENU_PATTERN),
            ],
            WAIT_ALLOW_MASS: [
                CallbackQueryHandler(on_nav_click, pattern=CB_NAV_PATTERN),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_allow_mass),
            ],
            WAIT_BLOCK_MASS: [
                CallbackQueryHandler(on_nav_click, pattern=CB_NAV_PATTERN),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_block_mass),
            ],
            WAIT_DELETE_MASS: [
                CallbackQueryHandler(on_nav_click, pattern=CB_NAV_PATTERN),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_delete_mass),
            ],
            WAIT_ADMIN_ONE: [
                CallbackQueryHandler(on_nav_click, pattern=CB_NAV_PATTERN),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_one),
            ],
            WAIT_UNADMIN_ONE: [
                CallbackQueryHandler(on_nav_click, pattern=CB_NAV_PATTERN),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_unadmin_one),
            ],
        },
        fallbacks=[CommandHandler("access", cmd_access)],
        name="access",
        persistent=False,
        per_user=True,
        per_chat=True,
        # InlineKeyboard callbacks привязаны к сообщению; per_message=True стабилизирует state-machine.
        per_message=True,
        allow_reentry=True,
    )
    app.add_handler(conv)
