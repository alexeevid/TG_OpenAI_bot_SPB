from __future__ import annotations

import re
from typing import List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

from ..services.authz_service import AuthzService
from ..db.repo_access import AccessRepo


MENU, WAIT_ALLOW_MASS, WAIT_BLOCK_MASS, WAIT_DELETE_MASS, WAIT_ADMIN_ONE, WAIT_UNADMIN_ONE = range(6)
CB_NS = "acc"


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    az: AuthzService | None = context.bot_data.get("svc_authz")
    uid = update.effective_user.id if update.effective_user else None
    return bool(az and uid is not None and az.is_admin(uid))


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Optional[AccessRepo]:
    return context.bot_data.get("repo_access")


def _kbd_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Добавить (массово)", callback_data=f"{CB_NS}:allow_mass"),
                InlineKeyboardButton("⛔ Заблокировать (массово)", callback_data=f"{CB_NS}:block_mass"),
            ],
            [
                InlineKeyboardButton("👑 Назначить админом", callback_data=f"{CB_NS}:admin_one"),
                InlineKeyboardButton("✅ Снять админа", callback_data=f"{CB_NS}:unadmin_one"),
            ],
            [
                InlineKeyboardButton("🗑 Удалить записи (массово)", callback_data=f"{CB_NS}:delete_mass"),
            ],
            [
                InlineKeyboardButton("📋 Список", callback_data=f"{CB_NS}:list"),
                InlineKeyboardButton("✖ Закрыть", callback_data=f"{CB_NS}:close"),
            ],
        ]
    )


def _kbd_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ Отмена", callback_data=f"{CB_NS}:cancel")]])


def _extract_ids(update: Update, text: str) -> List[int]:
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

    return list(dict.fromkeys(ids))


def _format_list(repo: AccessRepo) -> str:
    rows = repo.list(limit=200)
    header = "📋 Список доступов"
    if not rows:
        return header + "\n\n(пусто)"

    lines = [header, ""]
    for r in rows:
        flags = []
        flags.append("✅" if r.is_allowed else "⛔")
        if r.is_admin:
            flags.append("👑")
        note = f" — {r.note}" if r.note else ""
        lines.append(f"• {r.tg_id} {' '.join(flags)}{note}")
    return "\n".join(lines)


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update, context):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return ConversationHandler.END

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен.")
        return ConversationHandler.END

    args = context.args or []
    if args:
        sub = args[0].lower()

        if sub == "list":
            await update.effective_message.reply_text(_format_list(repo))
            return ConversationHandler.END

    await update.effective_message.reply_text(
        "🔐 Управление доступом",
        reply_markup=_kbd_menu(),
    )
    return MENU


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    action = q.data.split(":")[1]

    if action == "allow_mass":
        await q.edit_message_text(
            "Отправь список tg_id для ДОБАВЛЕНИЯ (через пробел / перенос строки).",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_ALLOW_MASS

    if action == "block_mass":
        await q.edit_message_text(
            "Отправь список tg_id для БЛОКИРОВКИ.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_BLOCK_MASS

    if action == "delete_mass":
        await q.edit_message_text(
            "Отправь список tg_id для УДАЛЕНИЯ записей.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_DELETE_MASS

    if action == "admin_one":
        await q.edit_message_text(
            "Отправь tg_id пользователя для назначения админом.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_ADMIN_ONE

    if action == "unadmin_one":
        await q.edit_message_text(
            "Отправь tg_id пользователя для снятия админа.",
            reply_markup=_kbd_cancel(),
        )
        return WAIT_UNADMIN_ONE

    if action == "list":
        await q.edit_message_text(_format_list(_repo(context)), reply_markup=_kbd_menu())
        return MENU

    if action in {"close", "cancel"}:
        await q.edit_message_text("Ок, закрыто.", reply_markup=None)
        return ConversationHandler.END

    return MENU


async def on_mass(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> int:
    repo = _repo(context)
    ids = _extract_ids(update, update.effective_message.text)

    if not ids:
        await update.effective_message.reply_text("⚠️ Не найдено ни одного tg_id.", reply_markup=_kbd_cancel())
        return MENU

    for tg_id in ids:
        if mode == "allow":
            repo.upsert(tg_id, allow=True)
        elif mode == "block":
            repo.upsert(tg_id, allow=False)
        elif mode == "delete":
            repo.delete(tg_id)

    await update.effective_message.reply_text(
        f"Готово. Обработано пользователей: {len(ids)}",
        reply_markup=_kbd_menu(),
    )
    return MENU


async def on_allow_mass(update, context): return await on_mass(update, context, "allow")
async def on_block_mass(update, context): return await on_mass(update, context, "block")
async def on_delete_mass(update, context): return await on_mass(update, context, "delete")


async def on_admin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    repo = _repo(context)
    ids = _extract_ids(update, update.effective_message.text)
    if not ids:
        await update.effective_message.reply_text("⚠️ Укажи tg_id.")
        return MENU

    repo.upsert(ids[0], allow=True, admin=True)
    await update.effective_message.reply_text("👑 Назначен админ.", reply_markup=_kbd_menu())
    return MENU


async def on_unadmin_one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    repo = _repo(context)
    ids = _extract_ids(update, update.effective_message.text)
    if not ids:
        await update.effective_message.reply_text("⚠️ Укажи tg_id.")
        return MENU

    cur = repo.get(ids[0])
    if cur:
        repo.upsert(ids[0], allow=cur.is_allowed, admin=False)

    await update.effective_message.reply_text("✅ Админ снят.", reply_markup=_kbd_menu())
    return MENU


def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("access", cmd_access)],
        states={
            MENU: [CallbackQueryHandler(on_menu, pattern=f"^{CB_NS}:")],
            WAIT_ALLOW_MASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_allow_mass)],
            WAIT_BLOCK_MASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_block_mass)],
            WAIT_DELETE_MASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_delete_mass)],
            WAIT_ADMIN_ONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_one)],
            WAIT_UNADMIN_ONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_unadmin_one)],
        },
        fallbacks=[],
        name="access",
        persistent=False,
    )
    app.add_handler(conv)
