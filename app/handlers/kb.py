from __future__ import annotations

from typing import List

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.dialog_kb_service import DialogKBService


HELP = """Команды Базы знаний (в рамках текущего диалога):
/kb — справка и статус
/kb on | off | auto — режим применения БЗ в диалоге
/kb list — список подключённых документов
/kb add <path|resource_id|id> — подключить документ к диалогу
/kb remove <id> — отключить документ от диалога
/kb enable <id> | /kb disable <id> — включить/исключить документ в контексте
/kb password <id> <пароль> — сохранить пароль PDF (только в этом диалоге)

Админские:
/kb scan — проверить новые/изменённые/удалённые файлы (глобально)
/kb sync — проиндексировать изменения (глобально)
/kb status — сводка статусов (глобально)
/kb reindex <path|resource_id|id> — переиндексировать один файл
"""


async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    az: AuthzService = context.bot_data.get("svc_authz")
    ds: DialogService = context.bot_data.get("svc_dialog")
    dkb: DialogKBService = context.bot_data.get("svc_dialog_kb")

    if not ds or not dkb:
        await update.message.reply_text("⚠️ Сервисы не настроены.")
        return

    dialog = ds.get_active_dialog(update.effective_user.id)
    if not dialog:
        await update.message.reply_text("⚠️ Активный диалог не найден. Используйте /dialogs.")
        return

    args = context.args or []
    sub = (args[0].lower() if args else "").strip()

    # --- no args -> help + status ---
    if not sub:
        mode = dkb.get_mode(dialog.id)
        attached = dkb.list_attached(dialog.id)
        enabled_count = sum(1 for x in attached if x.get("is_enabled") and x.get("is_active"))
        await update.message.reply_text(
            f"KB: mode={mode}, attached={len(attached)}, enabled_active={enabled_count}\n\n{HELP}"
        )
        return

    # --- mode ---
    if sub in ("on", "off", "auto"):
        mode = dkb.set_mode(dialog.id, sub.upper())
        await update.message.reply_text(f"✅ KB режим в диалоге: {mode}")
        return

    # --- list ---
    if sub == "list":
        items = dkb.list_attached(dialog.id)
        if not items:
            await update.message.reply_text("Пока нет подключённых документов. Используйте /kb add <path|resource_id|id>.")
            return
        lines: List[str] = ["Подключённые документы:"]
        for it in items:
            did = it["document_id"]
            st = []
            st.append("active" if it.get("is_active") else "inactive")
            st.append("enabled" if it.get("is_enabled") else "disabled")
            title = it.get("title") or ""
            path = it.get("path") or ""
            rid = it.get("resource_id") or ""
            lines.append(f"- id={did} [{', '.join(st)}] {title} | {path} | {rid}")
        await update.message.reply_text("\n".join(lines))
        return

    # --- add ---
    if sub == "add":
        ref = " ".join(args[1:]).strip() if len(args) > 1 else ""
        if not ref:
            await update.message.reply_text("Использование: /kb add <path|resource_id|id>")
            return
        ok, msg = dkb.attach_by_ref(dialog.id, ref)
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        return

    # --- remove ---
    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Использование: /kb remove <id>")
            return
        ok, msg = dkb.detach(dialog.id, int(args[1]))
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        return

    # --- enable/disable ---
    if sub in ("enable", "disable"):
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("Использование: /kb enable <id>  |  /kb disable <id>")
            return
        enabled = (sub == "enable")
        ok, msg = dkb.enable(dialog.id, int(args[1]), enabled)
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        return

    # --- password ---
    if sub == "password":
        if len(args) < 3 or not args[1].isdigit():
            await update.message.reply_text("Использование: /kb password <id> <пароль>")
            return
        doc_id = int(args[1])
        password = " ".join(args[2:]).strip()
        ok, msg = dkb.set_pdf_password(dialog.id, doc_id, password)
        await update.message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        return

    # --- admin commands (scan/sync/status/reindex) ---
    # Делегируем в существующий синкер, если он подключён.
    syncer = context.bot_data.get("svc_kb_syncer")
    if sub in ("scan", "sync", "status", "reindex"):
        # access control: только админы
        if az and not az.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещён (только админы).")
            return

        if not syncer:
            await update.message.reply_text("⚠️ Сервис синхронизации БЗ не настроен.")
            return

        try:
            if sub == "scan":
                report = syncer.scan()
                await update.message.reply_text(
                    f"KB scan: new={len(report.new)} outdated={len(report.outdated)} deleted={len(report.deleted)}"
                )
                return

            if sub == "sync":
                report, ok, fail, deleted = syncer.sync()
                await update.message.reply_text(
                    f"KB sync: ok={ok} fail={fail} deleted={deleted} | new={len(report.new)} outdated={len(report.outdated)}"
                )
                return

            if sub == "status":
                st = syncer.status_summary()
                await update.message.reply_text("KB status:\n" + "\n".join(f"- {k}: {v}" for k, v in st.items()))
                return

            if sub == "reindex":
                ref = " ".join(args[1:]).strip() if len(args) > 1 else ""
                if not ref:
                    await update.message.reply_text("Использование: /kb reindex <path|resource_id|id>")
                    return
                ok = syncer.reindex_one(ref)
                await update.message.reply_text("✅ Переиндексация выполнена." if ok else "⚠️ Документ не найден/не обработан.")
                return

        except Exception:
            # В проде лучше log.exception, но не будем “мелкими правками” менять errors handler.
            await update.message.reply_text("⚠️ Внутренняя ошибка. Подробности записаны в лог.")
            return

    # --- fallback: help ---
    await update.message.reply_text(HELP)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_handler))
