from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..db.repo_access import AccessRepo


def _parse_target_id(update: Update, args: list[str]) -> int | None:
    # 1) если команда в ответ на сообщение — берём пользователя из reply
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        return int(update.message.reply_to_message.from_user.id)

    # 2) иначе ждём tg_id аргументом
    if not args:
        return None

    raw = args[0].strip()

    # username типа @name здесь не резолвим — Telegram API не даёт “поиск username->id”
    # (можно будет сделать, когда начнём хранить tg_username при /start)
    try:
        return int(raw)
    except ValueError:
        return None


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    az: AuthzService = context.bot_data.get("svc_authz")
    if not az or not update.effective_user or not az.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return

    repo: AccessRepo = context.bot_data.get("repo_access")
    if not repo:
        await update.effective_message.reply_text("⚠️ repo_access не подключен в main.py")
        return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "🔐 /access — управление доступом (только админ)\n\n"
            "Команды:\n"
            "• /access list\n"
            "• /access allow <tg_id> [note]\n"
            "• /access block <tg_id> [note]\n"
            "• /access admin <tg_id> [note]\n"
            "• /access unadmin <tg_id>\n"
            "• /access delete <tg_id>\n\n"
            "Лайфхак: можно выполнить команду *ответом* на сообщение пользователя — тогда tg_id не нужен.",
            parse_mode="Markdown",
        )
        return

    sub = args[0].lower().strip()

    if sub == "list":
        rows = repo.list()
        db_mode = repo.has_any_entries()
        header = "📋 Доступы (DB-режим: включён ✅)\n" if db_mode else "📋 Доступы (DB-режим: выключен ⛔ — таблица пуста)\n"
        if not rows:
            await update.effective_message.reply_text(header + "\n(пусто)")
            return
        lines = [header]
        for r in rows:
            flags = []
            flags.append("✅allow" if r.is_allowed else "⛔block")
            if r.is_admin:
                flags.append("👑admin")
            note = f" — {r.note}" if r.note else ""
            lines.append(f"• {r.tg_id}: {' '.join(flags)}{note}")
        await update.effective_message.reply_text("\n".join(lines))
        return

    if sub in {"allow", "block", "admin", "unadmin", "delete"}:
        # Для allow/block/admin/unadmin/delete целевой id берём либо из reply, либо вторым аргументом
        target = _parse_target_id(update, args[1:] if sub == "list" else args[1:])  # безопасно
        # но выше для sub у нас args[0]=sub, значит tg_id в args[1], note в args[2:]
        target = _parse_target_id(update, args[1:])  # корректно для всех сабкоманд

        if target is None:
            await update.effective_message.reply_text(
                "⚠️ Не смог определить пользователя.\n"
                "Варианты:\n"
                "1) /access allow <tg_id>\n"
                "2) ответьте на сообщение пользователя и выполните /access allow"
            )
            return

        note = " ".join(args[2:]).strip() if len(args) > 2 else ""

        if sub == "allow":
            repo.upsert(target, allow=True, admin=False, note=note)
            await update.effective_message.reply_text(f"✅ Доступ разрешён: {target}")
            return

        if sub == "block":
            repo.upsert(target, allow=False, admin=False, note=note)
            await update.effective_message.reply_text(f"⛔ Доступ запрещён: {target}")
            return

        if sub == "admin":
            repo.set_admin(target, is_admin=True, note=note)
            await update.effective_message.reply_text(f"👑 Назначен админ: {target}")
            return

        if sub == "unadmin":
            repo.set_admin(target, is_admin=False)
            await update.effective_message.reply_text(f"✅ Админ снят: {target}")
            return

        if sub == "delete":
            ok = repo.delete(target)
            await update.effective_message.reply_text("🗑 Запись удалена." if ok else "ℹ️ Записи не было.")
            return

    await update.effective_message.reply_text("⚠️ Неизвестная команда. Напишите /access для справки.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("access", cmd_access))
