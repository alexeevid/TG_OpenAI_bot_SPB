# app/handlers/kb.py
from __future__ import annotations

import asyncio
import logging
import time
from math import ceil

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..services.authz_service import AuthzService
from ..services.dialog_service import DialogService
from ..services.dialog_kb_service import DialogKBService
from ..db.repo_kb import KBRepo
from .kb_ui import kb_select_show

log = logging.getLogger(__name__)


HELP = """База знаний (встроена в диалоги)

Основное:
/kb                 — справка и статус
/kb select          — выбрать документы для ТЕКУЩЕГО диалога (интерфейс)
/kb list            — список документов, подключённых к диалогу
/kb on|off|auto     — режим применения БЗ в диалоге (best practice: AUTO)

/kb catalog [стр]   — каталог документов БЗ (глобально)
/kb stats           — статистика БЗ (глобально)
/kb stats dialog    — статистика БЗ для текущего диалога

Пароли PDF (в рамках диалога):
/kb password <doc_id> <пароль>

Админ (если настроен syncer):
/kb scan | /kb sync | /kb status
"""


def _short_name(path: str) -> str:
    p = (path or "").strip()
    return p.split("/")[-1] if p else ""


async def kb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    az: AuthzService = context.bot_data.get("svc_authz")
    ds: DialogService = context.bot_data.get("svc_dialog")
    dkb: DialogKBService = context.bot_data.get("svc_dialog_kb")
    kb_repo: KBRepo = context.bot_data.get("repo_kb")

    if az and not az.is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Доступ запрещен.")
        return

    if not ds or not dkb or not kb_repo:
        await update.effective_message.reply_text("⚠️ Сервисы БЗ не настроены.")
        return

    d = ds.get_active_dialog(update.effective_user.id)
    if not d:
        await update.effective_message.reply_text("⚠️ Активный диалог не найден. Используйте /dialogs.")
        return

    args = context.args or []
    sub = (args[0].lower().strip() if args else "")

    # --- /kb ---
    if not sub:
        mode = dkb.get_mode(d.id)
        attached = dkb.list_attached(d.id)
        enabled = sum(1 for x in attached if x.get("is_enabled"))
        await update.effective_message.reply_text(
            f"Диалог #{d.id}\nKB mode: {mode}\nПодключено: {len(attached)} (включено: {enabled})\n\n{HELP}"
        )
        return

    # --- mode ---
    if sub in ("on", "off", "auto"):
        try:
            m = dkb.set_mode(d.id, sub.upper())
            await update.effective_message.reply_text(f"✅ KB режим: {m}")
        except Exception as e:
            log.exception("kb mode failed: %s", e)
            await update.effective_message.reply_text("⚠️ Не удалось изменить режим KB.")
        return

    # --- select (UI) ---
    if sub == "select":
        await kb_select_show(update, context, page=1)
        return

    # --- list (attached) ---
    if sub == "list":
        try:
            items = dkb.list_attached(d.id)
            if not items:
                await update.effective_message.reply_text("Нет подключённых документов. Используйте /kb select.")
                return
            lines = ["Документы диалога:"]
            for it in items:
                did = int(it["document_id"])
                st = "✅" if it.get("is_enabled") else "➖"
                title = (it.get("title") or "").strip()
                path = (it.get("path") or "").strip()
                label = title if title else path
                lines.append(f"{st} id={did} — {label}")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb list failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка списка KB.")
        return

    # --- catalog ---
    if sub == "catalog":
        try:
            page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
            items, total = kb_repo.catalog(page=page, page_size=10, search="")
            pages = max(1, ceil(total / 10))

            attached = dkb.list_attached(d.id)
            attached_map = {int(x["document_id"]): bool(x["is_enabled"]) for x in attached}

            def badge(doc_id: int) -> str:
                if doc_id not in attached_map:
                    return "⬜"
                return "✅" if attached_map[doc_id] else "➖"

            if not items:
                await update.effective_message.reply_text("Каталог пуст. Сначала выполните /kb sync (админ).")
                return

            lines = [f"Каталог БЗ: страница {page}/{pages}, всего документов: {total}"]
            for it in items:
                did = int(it["id"])
                chunks = int(it.get("chunks") or 0)
                title = (it.get("title") or "").strip()
                path = (it.get("path") or "").strip()
                label = title if title else path
                lines.append(f"{badge(did)} id={did} · chunks={chunks} · {label}")

            lines.append("\nДля выбора документов используйте: /kb select")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb catalog failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка каталога KB.")
        return

    # --- stats ---
    if sub == "stats":
        try:
            scope = (args[1].lower().strip() if len(args) > 1 else "")
            if scope == "dialog":
                allowed = dkb.allowed_document_ids(d.id)
                st = kb_repo.stats_for_document_ids(allowed)
                attached = dkb.list_attached(d.id)
                enabled = sum(1 for x in attached if x.get("is_enabled"))
                await update.effective_message.reply_text(
                    "KB stats (диалог)\n"
                    f"- dialog_id: {d.id}\n"
                    f"- attached: {len(attached)}\n"
                    f"- enabled: {enabled}\n"
                    f"- documents_in_scope: {st.get('documents', 0)}\n"
                    f"- chunks_in_scope: {st.get('chunks', 0)}"
                )
            else:
                st = kb_repo.stats_global()
                lines = [
                    "KB stats (глобально)",
                    f"- documents: {st['documents']}",
                    f"- chunks: {st['chunks']}",
                    "",
                    "Top documents by chunks:",
                ]
                for r in st["top_docs"]:
                    title = (r.get("title") or "").strip()
                    path = (r.get("path") or "").strip()
                    label = title if title else path
                    lines.append(f"- id={r['id']} · chunks={r['chunks']} · {label}")
                await update.effective_message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("kb stats failed: %s", e)
            await update.effective_message.reply_text("⚠️ Ошибка статистики KB.")
        return

    # --- password ---
    if sub == "password":
        try:
            if len(args) < 3 or not args[1].isdigit():
                await update.effective_message.reply_text("Использование: /kb password <doc_id> <пароль>")
                return
            doc_id = int(args[1])
            pwd = " ".join(args[2:]).strip()
            dkb.set_pdf_password(d.id, doc_id, pwd)
            await update.effective_message.reply_text(f"✅ Пароль сохранён для doc_id={doc_id} (только в этом диалоге).")
        except Exception as e:
            log.exception("kb password failed: %s", e)
            await update.effective_message.reply_text("⚠️ Не удалось сохранить пароль.")
        return

    # --- admin: scan/sync/status (if syncer exists) ---
    if sub in ("scan", "sync", "status"):
        if az and not az.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Только для админов.")
            return
        syncer = context.bot_data.get("svc_syncer")
        if not syncer:
            await update.effective_message.reply_text("⚠️ Сервис синхронизации не настроен.")
            return

        try:
            if sub == "scan":
                rep = syncer.scan()
                await update.effective_message.reply_text(
                    f"KB scan: new={len(rep.new)} outdated={len(rep.outdated)} deleted={len(rep.deleted)}"
                )
                return

            if sub == "status":
                st = syncer.status_summary()
                await update.effective_message.reply_text(
                    "KB status:\n" + "\n".join(f"- {k}: {v}" for k, v in st.items())
                )
                return

            if sub == "sync":
                # 1) Сразу отвечаем + прогресс будем редактировать это сообщение
                msg = await update.effective_message.reply_text("KB sync: стартовал… (это может занять несколько минут)")

                loop = asyncio.get_running_loop()
                start_ts = time.time()
                last_text = {"value": ""}

                async def _safe_edit(text: str) -> None:
                    # не дёргаем edit если текст тот же
                    if text == last_text["value"]:
                        return
                    last_text["value"] = text
                    try:
                        await msg.edit_text(text)
                    except Exception:
                        # редактирование может падать (rate limits / message not modified) — не критично
                        pass

                def progress_cb(processed: int, total: int, path: str, ok: int, fail: int) -> None:
                    elapsed = int(time.time() - start_ts)
                    name = _short_name(path) if path and path != "<done>" else ""
                    line = f"KB sync: {processed}/{total} | ok={ok} fail={fail} | {elapsed}s"
                    if name:
                        line += f"\n{ name }"
                    # вызываем edit из thread-safe контекста
                    asyncio.run_coroutine_threadsafe(_safe_edit(line), loop)

                # 2) НЕ блокируем обработку апдейтов: запускаем sync в executor
                def _run_sync():
                    return syncer.sync(progress_cb=progress_cb)

                rep, ok, fail, deleted = await loop.run_in_executor(None, _run_sync)

                elapsed = int(time.time() - start_ts)
                final = (
                    f"KB sync: ✅ готово за {elapsed}s\n"
                    f"- ok: {ok}\n"
                    f"- fail: {fail}\n"
                    f"- deleted: {deleted}\n"
                    f"- new: {len(rep.new)}\n"
                    f"- outdated: {len(rep.outdated)}"
                )
                await _safe_edit(final)
                return

        except Exception as e:
            log.exception("admin kb op failed: %s", e)
            await update.effective_message.reply_text("⚠️ Внутренняя ошибка KB (см. лог).")
            return

    await update.effective_message.reply_text(HELP)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("kb", kb_handler))
