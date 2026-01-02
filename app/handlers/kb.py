from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..kb.syncer import KBSyncer


async def _deny(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔ Доступ запрещен.")


async def kb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    az: AuthzService = context.bot_data.get("svc_authz")
    uid = update.effective_user.id if update.effective_user else None
    if az and uid is not None and not az.is_allowed(uid):
        await _deny(update)
        return

    syncer: KBSyncer = context.bot_data.get("svc_syncer")
    if not syncer:
        await update.message.reply_text("⚠️ База знаний не настроена.")
        return

    args = context.args or []
    sub = (args[0].lower() if args else "help")

    if sub in {"help", "h", "?"}:
        await update.message.reply_text(
            "\n".join(
                [
                    "Команды Базы знаний:",
                    "/kb <запрос> — поиск по БЗ",
                    "/kb scan — проверить новые/изменённые/удалённые файлы",
                    "/kb sync — проиндексировать изменения",
                    "/kb status — сводка статусов",
                    "/kb reindex <path|resource_id> — переиндексировать один файл",
                    "/update — то же что /kb sync",
                ]
            )
        )
        return

    if sub == "scan":
        rep = syncer.scan()
        await update.message.reply_text(
            "\n".join(
                [
                    "SCAN завершён:",
                    f"  new: {len(rep.new)}",
                    f"  outdated: {len(rep.outdated)}",
                    f"  deleted: {len(rep.deleted_resource_ids)}",
                ]
            )
        )
        return

    if sub == "sync":
        rep, ok, fail, purged = syncer.sync()
        await update.message.reply_text(
            "\n".join(
                [
                    "SYNC завершён:",
                    f"  new: {len(rep.new)}",
                    f"  outdated: {len(rep.outdated)}",
                    f"  deleted: {len(rep.deleted_resource_ids)}",
                    f"  purged_deleted: {purged}",
                    f"  indexed_ok: {ok}",
                    f"  indexed_failed: {fail}",
                ]
            )
        )
        return

    if sub == "status":
        summ = syncer.status_summary()
        lines = ["KB status:"]
        for k in sorted(summ.keys()):
            lines.append(f"  {k}: {summ[k]}")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "reindex":
        if len(args) < 2:
            await update.message.reply_text("Использование: /kb reindex <resource_id|path>")
            return
        key = " ".join(args[1:]).strip()
        ok = syncer.reindex_one(key)
        await update.message.reply_text("Готово." if ok else "Не найдено или ошибка (см. /kb status).")
        return

    # default: search
    query = " ".join(args) if args else ""
    if not query:
        await update.message.reply_text("Использование: /kb <запрос> (или /kb help)")
        return

    rag = context.bot_data.get("svc_rag")
    if not rag:
        await update.message.reply_text("⚠️ RAG сервис не настроен.")
        return

    results = rag.retrieve(query, dialog_id=0, top_k=5)
    if not results:
        await update.message.reply_text("Ничего не найдено.")
        return

    lines = [f"Найдено фрагментов: {len(results)}"]
    for i, chunk in enumerate(results, start=1):
        t = chunk.text
        if len(t) > 400:
            t = t[:400] + "..."
        lines.append(f"{i}. {t}")
    await update.message.reply_text("\n".join(lines))


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.args = ["sync"]
    await kb_cmd(update, context)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
