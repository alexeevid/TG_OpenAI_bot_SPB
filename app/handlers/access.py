from __future__ import annotations

import logging
import re
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..db.repo_access import AccessRepo

log = logging.getLogger(__name__)

CB_NS = "acc"

# access ui states (stored in chat_data)
ST_NONE = None
ST_ALLOW_MASS = "allow_mass"
ST_BLOCK_MASS = "block_mass"
ST_DELETE_MASS = "delete_mass"
ST_ADMIN_ONE = "admin_one"
ST_UNADMIN_ONE = "unadmin_one"


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Optional[AccessRepo]:
    return context.application.bot_data.get("repo_access") or context.bot_data.get("repo_access")


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    az = context.application.bot_data.get("svc_authz") or context.bot_data.get("svc_authz")
    uid = update.effective_user.id if update.effective_user else None
    return bool(az and uid is not None and az.is_admin(uid))


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: Optional[str]) -> None:
    context.chat_data["access_state"] = state


def _get_state(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    return context.chat_data.get("access_state")


def _kbd_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Добавить (массово)", callback_data=f"{CB_NS}:allow_mass"),
                InlineKeyboardButton("Заблокировать (массово)", callback_data=f"{CB_NS}:block_mass"),
            ],
            [
                InlineKeyboardButton("Назначить админом", callback_data=f"{CB_NS}:admin_one"),
                InlineKeyboardButton("Снять админа", callback_data=f"{CB_NS}:unadmin_one"),
            ],
            [
                InlineKeyboardButton("Удалить записи", callback_data=f"{CB_NS}:delete_mass"),
                InlineKeyboardButton("Показать список", callback_data=f"{CB_NS}:list"),
            ],
            [
                InlineKeyboardButton("Отмена", callback_data=f"{CB_NS}:cancel"),
                InlineKeyboardButton("Закрыть", callback_data=f"{CB_NS}:close"),
            ],
        ]
    )


def _extract_ids_from_text(update: Update, text: str) -> List[int]:
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

    seen = set()
    out: List[int] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_target_id(update: Update, args: List[str]) -> Optional[int]:
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        try:
            return int(msg.reply_to_message.from_user.id)
        except Exception:
            return None

    for a in args or []:
        m = re.search(r"\d{5,}", a or "")
        if m:
            try:
                return int(m.group(0))
            except Exception:
                continue
    return None


def _format_list(repo: AccessRepo) -> str:
    try:
        rows = repo.list(limit=200)
    except Exception:
        rows = []

    try:
        db_mode = repo.has_any_entries()
    except Exception:
        db_mode = False

    header = "Доступы (DB-режим: включён)" if db_mode else "Доступы (DB-режим: выключен — таблица пуста)"
    if not rows:
        return header + "\n\n(пусто)"

    lines = [header, ""]
    for r in rows:
        flags = []
        flags.append("allow" if r.is_allowed else "block")
        if r.is_admin:
            flags.append("admin")
        note = f" — {r.note}" if getattr(r, "note", "") else ""
        lines.append(f"- {r.tg_id}: {' '.join(flags)}{note}")
    return "\n".join(lines)


async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if update.effective_chat:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _typing(update, context)

    repo = _repo(context)
    if not repo:
        await update.effective_message.reply_text("repo_access не подключен (проверь bootstrap/main).")
        return

    # лог видимый в проде — по нему мы точно понимаем, что /access дошёл
    try:
        uid = update.effective_user.id if update.effective_user else None
        log.warning("ACCESS_CMD hit: uid=%s", uid)
    except Exception:
        pass

    if not _is_admin(update, context):
        await update.effective_message.reply_text("Доступ запрещен.")
        return

    # CLI режим
    args = context.args or []
    if args:
        sub = args[0].lower().strip()

        if sub == "list":
            await update.effective_message.reply_text(_format_list(repo))
            return

        if sub in {"allow", "block", "admin", "unadmin", "delete"}:
            target = _parse_target_id(update, args[1:])
            if not target:
                await update.effective_message.reply_text("Не вижу tg_id. Пример: /access allow 123456789")
                return

            note = " ".join(args[2:]).strip() if len(args) > 2 else ""

            if sub == "allow":
                repo.upsert(target, allow=True, admin=False, note=note)
                await update.effective_message.reply_text(f"OK: allow {target}")
                return

            if sub == "block":
                repo.upsert(target, allow=False, admin=False, note=note)
                await update.effective_message.reply_text(f"OK: block {target}")
                return

            if sub == "admin":
                repo.upsert(target, allow=True, admin=True, note=note)
                await update.effective_message.reply_text(f"OK: admin {target}")
                return

            if sub == "unadmin":
                cur = repo.get(target)
                allow = bool(cur.is_allowed) if cur else True
                repo.upsert(target, allow=allow, admin=False, note=note)
                await update.effective_message.reply_text(f"OK: unadmin {target}")
                return

            if sub == "delete":
                ok = repo.delete(target)
                await update.effective_message.reply_text(f"{'OK' if ok else 'NOT_FOUND'}: delete {target}")
                return

        await update.effective_message.reply_text("Неизвестная команда. Пример: /access list")
        return

    # UI режим
    _set_state(context, ST_NONE)
    await update.effective_message.reply_text("Управление доступами", reply_markup=_kbd_menu())


async def on_access_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    repo = _repo(context)
    if not repo:
        try:
            await q.edit_message_text("repo_access не подключен.", reply_markup=None)
        except Exception:
            pass
        return

    if not _is_admin(update, context):
        try:
            await q.edit_message_text("Доступ запрещен.", reply_markup=None)
        except Exception:
            pass
        return

    data = q.data or ""
    if not data.startswith(f"{CB_NS}:"):
        return

    action = data.split(":", 1)[1].strip()

    if action == "list":
        _set_state(context, ST_NONE)
        await q.edit_message_text(_format_list(repo), reply_markup=_kbd_menu())
        return

    if action == "cancel":
        _set_state(context, ST_NONE)
        await q.edit_message_text("Управление доступами", reply_markup=_kbd_menu())
        return

    if action == "close":
        _set_state(context, ST_NONE)
        await q.edit_message_text("Закрыто.", reply_markup=None)
        return

    if action == "allow_mass":
        _set_state(context, ST_ALLOW_MASS)
        await q.edit_message_text("Пришли tg_id (списком).", reply_markup=_kbd_menu())
        return

    if action == "block_mass":
        _set_state(context, ST_BLOCK_MASS)
        await q.edit_message_text("Пришли tg_id (списком).", reply_markup=_kbd_menu())
        return

    if action == "delete_mass":
        _set_state(context, ST_DELETE_MASS)
        await q.edit_message_text("Пришли tg_id (списком).", reply_markup=_kbd_menu())
        return

    if action == "admin_one":
        _set_state(context, ST_ADMIN_ONE)
        await q.edit_message_text("Пришли tg_id (один) или reply.", reply_markup=_kbd_menu())
        return

    if action == "unadmin_one":
        _set_state(context, ST_UNADMIN_ONE)
        await q.edit_message_text("Пришли tg_id (один) или reply.", reply_markup=_kbd_menu())
        return


async def on_access_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ВАЖНО: этот handler block=False. Он не мешает общему text.py.
    Но если мы в access_state != None — он отрабатывает и фактически “забирает” сообщение.
    """
    state = _get_state(context)
    if not state:
        return  # не в режиме access — пропускаем

    repo = _repo(context)
    if not repo or not _is_admin(update, context):
        _set_state(context, ST_NONE)
        return

    await _typing(update, context)

    text = update.effective_message.text or ""
    ids = _extract_ids_from_text(update, text)
    if not ids:
        await update.effective_message.reply_text("Не вижу tg_id. Пришли числа (5+ цифр) или reply.", reply_markup=_kbd_menu())
        return

    if state in (ST_ALLOW_MASS, ST_BLOCK_MASS, ST_DELETE_MASS):
        ok = 0
        for tg_id in ids:
            try:
                if state == ST_DELETE_MASS:
                    if repo.delete(tg_id):
                        ok += 1
                else:
                    repo.upsert(tg_id, allow=(state == ST_ALLOW_MASS), admin=False, note="ui")
                    ok += 1
            except Exception:
                pass

        _set_state(context, ST_NONE)
        await update.effective_message.reply_text(f"Готово: {ok}/{len(ids)}", reply_markup=_kbd_menu())
        return

    if state in (ST_ADMIN_ONE, ST_UNADMIN_ONE):
        target = ids[0]
        try:
            repo.upsert(target, allow=True, admin=(state == ST_ADMIN_ONE), note="ui")
            _set_state(context, ST_NONE)
            await update.effective_message.reply_text("Готово.", reply_markup=_kbd_menu())
        except Exception:
            _set_state(context, ST_NONE)
            await update.effective_message.reply_text("Не удалось выполнить операцию.", reply_markup=_kbd_menu())
        return


def register(app: Application) -> None:
    # жёсткий приоритет
    app.add_handler(CommandHandler("access", cmd_access), group=-10)
    app.add_handler(CallbackQueryHandler(on_access_menu_click, pattern=rf"^{CB_NS}:"), group=-10)

    # ВАЖНО: block=False — не ломаем обычные текстовые ответы.
    # Но если включён access_state, мы обрабатываем сообщение.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_access_text, block=False), group=-10)
