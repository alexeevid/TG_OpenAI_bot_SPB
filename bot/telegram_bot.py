from __future__ import annotations

import logging
from datetime import datetime
from io import BytesIO

# PTB 20.4 не содержит BufferedInputFile. Делаем совместимый импорт.
try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile, InputFile
    )
    HAS_BUFFERED = True
except Exception:  # pragma: no cover
    from telegram import (  # type: ignore
        Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
    )
    HAS_BUFFERED = False

from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, ContextTypes,
    MessageHandler, CallbackQueryHandler, filters
)
from sqlalchemy import text

from bot.settings import load_settings
from bot.db.session import SessionLocal  # engine импортируем внутри apply_migrations_if_needed

log = logging.getLogger(__name__)
settings = load_settings()

# --- Авто-миграция при старте (если нет таблиц) ---
def apply_migrations_if_needed(force: bool = False) -> None:
    """
    Если таблицы отсутствуют (или force=True), запускаем alembic upgrade head.
    Работает без консоли Railway.
    """
    try:
        from sqlalchemy import text
        from bot.db.session import engine
        need = True
        if not force:
            # Проверяем наличие ключевой таблицы
            with engine.connect() as conn:
                exists = conn.execute(text("SELECT to_regclass('public.users')")).scalar()
                need = not bool(exists)

        if need:
            log.info("Auto-migrate: applying Alembic migrations...")
            # Настраиваем Alembic программно
            import os
            from alembic.config import Config
            from alembic import command
            cfg = Config("alembic.ini")  # файл лежит в корне проекта
            os.environ["DATABASE_URL"] = settings.database_url  # чтобы Alembic знал, куда подключаться
            command.upgrade(cfg, "head")
            log.info("Auto-migrate: done")
        else:
            log.info("Auto-migrate: tables already present")
    except Exception:
        log.exception("Auto-migrate failed")

# ---------- helpers ----------
def _exec_scalar(db, sql: str, **params):
    return db.execute(text(sql), params).scalar()

def _exec_all(db, sql: str, **params):
    return db.execute(text(sql), params).all()

def _ensure_user(db, tg_id: int) -> int:
    uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg", tg=tg_id)
    if uid:
        return uid
    uid = _exec_scalar(
        db,
        """
        INSERT INTO users (tg_user_id, is_admin, is_allowed, lang)
        VALUES (:tg, FALSE, TRUE, 'ru')
        RETURNING id
        """, tg=tg_id,
    )
    return uid

def _ensure_dialog(db, user_id: int) -> int:
    did = _exec_scalar(
        db,
        """
        SELECT id FROM dialogs
        WHERE user_id=:u AND is_deleted=FALSE
        ORDER BY created_at DESC
        LIMIT 1
        """, u=user_id,
    )
    if did:
        return did
    did = _exec_scalar(
        db,
        """
        INSERT INTO dialogs (user_id, title, style, model, is_deleted)
        VALUES (:u, :t, 'expert', :m, FALSE)
        RETURNING id
        """, u=user_id, t=datetime.now().strftime("%Y-%m-%d | диалог"), m=settings.openai_model,
    )
    return did

def _is_admin(tg_id: int) -> bool:
    try:
        ids = [int(x.strip()) for x in (settings.admin_user_ids or "").split(",") if x.strip()]
        return tg_id in ids
    except Exception:
        return False

# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id if update.effective_chat else None
        user = update.effective_user
        if user:
            with SessionLocal() as db:
                uid = _ensure_user(db, user.id)
                _ensure_dialog(db, uid)
        text = (
            "Привет! Я помогу искать ответы в документах из БЗ.\n"
            "Откройте /kb (кнопки внутри) или задайте вопрос.\n\n"
            "/help — список команд."
        )
        if update.message:
            await update.message.reply_text(text)
        elif chat_id is not None:
            await context.bot.send_message(chat_id, text)
    except Exception:
        log.exception("start failed")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start /help /reset /stats\n"
        "/dialogs, /dialog <id>\n"
        "/kb, /kb_diag\n"
        "/model, /mode\n"
        "/img <prompt>\n"
        "/web <query>\n"
        "/whoami, /grant <id>, /revoke <id>"
    )

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        await update.message.reply_text("✅ OK: DB connection")
    except Exception:
        log.exception("health failed")
        await update.message.reply_text("❌ FAIL: DB connection")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
            dialogs = _exec_scalar(db, "SELECT COUNT(*) FROM dialogs") or 0
            messages = _exec_scalar(db, "SELECT COUNT(*) FROM messages") or 0
            docs = _exec_scalar(db, "SELECT COUNT(*) FROM kb_documents WHERE is_active") or 0
        await update.message.reply_text(
            f"Диалогов: {dialogs}\nСообщений: {messages}\nДокументов в БЗ: {docs}"
        )
    except Exception:
        log.exception("stats failed")
        await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

# ---------- dialogs ----------
async def dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = update.effective_user.id
        with SessionLocal() as db:
            uid = _ensure_user(db, tg_id)
            ds = _exec_all(
                db,
                """
                SELECT id, title
                FROM dialogs
                WHERE user_id=:u AND is_deleted=FALSE
                ORDER BY created_at DESC
                """, u=uid,
            )
        if not ds:
            await update.message.reply_text("Диалогов нет.")
            return
        rows = []
        for d_id, d_title in ds:
            rows.append([
                InlineKeyboardButton(f"📄 {d_title or d_id}", callback_data=f"dlg:open:{d_id}"),
                InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{d_id}"),
                InlineKeyboardButton("📤", callback_data=f"dlg:export:{d_id}"),
                InlineKeyboardButton("🗑", callback_data=f"dlg:delete:{d_id}"),
            ])
        await update.message.reply_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("dialogs failed")
        await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data.startswith("dlg:open:"):
            dlg_id = int(data.split(":")[-1])
            await q.edit_message_text(f"Открыт диалог #{dlg_id}")
            return

        if data.startswith("dlg:rename:"):
            dlg_id = int(data.split(":")[-1])
            context.user_data["rename_dialog_id"] = dlg_id
            await q.edit_message_text("Введите новое название диалога:")
            return

        if data.startswith("dlg:export:"):
            dlg_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                msgs = _exec_all(
                    db,
                    """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE dialog_id=:d
                    ORDER BY created_at
                    """, d=dlg_id,
                )
            lines = ["# Экспорт диалога", ""]
            for role, content, _ in msgs:
                who = "Пользователь" if role == "user" else "Бот"
                lines.append(f"**{who}:**\n{content}\n")
            data_bytes = "\n".join(lines).encode("utf-8")
            if HAS_BUFFERED:
                file = BufferedInputFile(data_bytes, filename=f"dialog_{dlg_id}.md")  # type: ignore
            else:
                file = InputFile(data_bytes, filename=f"dialog_{dlg_id}.md")  # type: ignore
            await q.message.reply_document(document=file, caption="Экспорт готов")
            return

        if data.startswith("dlg:delete:"):
            dlg_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                db.execute(text("UPDATE dialogs SET is_deleted=TRUE WHERE id=:d"), {"d": dlg_id})
                db.commit()
            await q.edit_message_text(f"Диалог #{dlg_id} удалён")
            return
    except Exception:
        log.exception("dialog_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка обработчика /dialogs. Попробуйте ещё раз.")
        except Exception:
            pass

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "rename_dialog_id" in context.user_data:
        dlg_id = context.user_data.pop("rename_dialog_id")
        new_title = (update.message.text or "").strip()[:100]
        if not new_title:
            await update.message.reply_text("Название пустое. Отменено.")
            return
        try:
            with SessionLocal() as db:
                db.execute(text("UPDATE dialogs SET title=:t WHERE id=:d"), {"t": new_title, "d": dlg_id})
                db.commit()
            await update.message.reply_text("Название сохранено.")
        except Exception:
            log.exception("rename dialog title failed")
            await update.message.reply_text("⚠ Не удалось сохранить название.")
        return
    await update.message.reply_text("Принято. (Текстовый роутер будет подключён к RAG после стабилизации UI.)")

# ---------- KB ----------
PAGE_SIZE = 8

def _exec_page_count(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

def _kb_keyboard(rows, page, pages, filter_name, admin: bool):
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"kb:list:{page-1}:{filter_name}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="kb:nop"))
    if page < pages:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"kb:list:{page+1}:{filter_name}"))

    filter_row = [
        InlineKeyboardButton(("🔵 " if filter_name == "all" else "") + "Все", callback_data="kb:list:1:all"),
        InlineKeyboardButton(("🔵 " if filter_name == "connected" else "") + "Подключённые", callback_data="kb:list:1:connected"),
        InlineKeyboardButton(("🔵 " if filter_name == "available" else "") + "Доступные", callback_data="kb:list:1:available"),
    ]

    keyboard = []
    keyboard.extend(rows)
    if nav:
        keyboard.append(nav)
    keyboard.append(filter_row)
    if admin:
        keyboard.append([InlineKeyboardButton("🔄 Синхронизация", callback_data="kb:sync")])
    keyboard.append([InlineKeyboardButton("📁 Статус БЗ", callback_data="kb:status")])
    return InlineKeyboardMarkup(keyboard)

def _kb_fetch(db, user_id: int, page: int, filter_name: str):
    dlg_id = _exec_scalar(
        db,
        """
        SELECT d.id
        FROM dialogs d
        WHERE d.user_id=:u AND d.is_deleted=FALSE
        ORDER BY d.created_at DESC
        LIMIT 1
        """, u=user_id,
    )
    if not dlg_id:
        dlg_id = _ensure_dialog(db, user_id)

    conn_ids = {row[0] for row in _exec_all(db,
        "SELECT document_id FROM dialog_kb_links WHERE dialog_id=:d", d=dlg_id)}

    where = "WHERE is_active"
    params = {}
    if filter_name == "connected":
        if conn_ids:
            where += " AND id = ANY(:ids)"
            params["ids"] = list(conn_ids)
        else:
            return dlg_id, [], 1, 1, conn_ids
    elif filter_name == "available" and conn_ids:
        where += " AND NOT (id = ANY(:ids))"
        params["ids"] = list(conn_ids)

    total = _exec_scalar(db, f"SELECT COUNT(*) FROM kb_documents {where}", **params) or 0
    pages = _exec_page_count(total)
    page = max(1, min(page, pages))

    rows = _exec_all(
        db,
        f"""
        SELECT id, path
        FROM kb_documents
        {where}
        ORDER BY path
        OFFSET :off LIMIT :lim
        """,
        off=(page - 1) * PAGE_SIZE, lim=PAGE_SIZE, **params
    )
    return dlg_id, rows, page, pages, conn_ids

async def kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = update.effective_user.id
        with SessionLocal() as db:
            uid = _ensure_user(db, tg_id)
            _ensure_dialog(db, uid)
            dlg_id, rows, page, pages, conn_ids = _kb_fetch(db, uid, 1, "all")
        buttons = []
        for d_id, path in rows:
            checked = "☑" if d_id in conn_ids else "☐"
            fname = path.split("/")[-1]
            buttons.append([InlineKeyboardButton(f"{checked} {fname}", callback_data=f"kb:toggle:{d_id}:{page}:all")])
        kb_markup = _kb_keyboard(buttons, page, pages, "all", admin=_is_admin(tg_id))
        await update.message.reply_text("Меню БЗ: выберите документы для подключения к активному диалогу.", reply_markup=kb_markup)
    except Exception:
        log.exception("kb failed")
        await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

async def kb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        tg_id = update.effective_user.id
        with SessionLocal() as db:
            uid = _ensure_user(db, tg_id)

            if data.startswith("kb:list:"):
                _, _, page, flt = data.split(":", 3)
                dlg_id, rows, page, pages, conn_ids = _kb_fetch(db, uid, int(page), flt)
                buttons = []
                for d_id, path in rows:
                    checked = "☑" if d_id in conn_ids else "☐"
                    fname = path.split("/")[-1]
                    buttons.append([InlineKeyboardButton(f"{checked} {fname}", callback_data=f"kb:toggle:{d_id}:{page}:{flt}")])
                kb_markup = _kb_keyboard(buttons, page, pages, flt, admin=_is_admin(tg_id))
                await q.edit_message_text("Меню БЗ: выберите документы для подключения к активному диалогу.", reply_markup=kb_markup)
                return

            if data.startswith("kb:toggle:"):
                _, _, doc_id, page, flt = data.split(":", 4)
                doc_id = int(doc_id)
                dlg_id = _exec_scalar(db,
                    """
                    SELECT id FROM dialogs WHERE user_id=:u AND is_deleted=FALSE
                    ORDER BY created_at DESC LIMIT 1
                    """, u=uid)
                if not dlg_id:
                    dlg_id = _ensure_dialog(db, uid)

                exist = _exec_scalar(db,
                    "SELECT id FROM dialog_kb_links WHERE dialog_id=:d AND document_id=:doc",
                    d=dlg_id, doc=doc_id)
                if exist:
                    db.execute(text("DELETE FROM dialog_kb_links WHERE id=:i"), {"i": exist})
                else:
                    db.execute(text(
                        "INSERT INTO dialog_kb_links (dialog_id, document_id, created_at) VALUES (:d, :doc, now())"
                    ), {"d": dlg_id, "doc": doc_id})
                db.commit()

                dlg_id, rows, page, pages, conn_ids = _kb_fetch(db, uid, int(page), flt)
                buttons = []
                for d_id, path in rows:
                    checked = "☑" if d_id in conn_ids else "☐"
                    fname = path.split("/")[-1]
                    buttons.append([InlineKeyboardButton(f"{checked} {fname}", callback_data=f"kb:toggle:{d_id}:{page}:{flt}")])
                kb_markup = _kb_keyboard(buttons, page, pages, flt, admin=_is_admin(tg_id))
                await q.edit_message_text("Меню БЗ: выберите документы для подключения к активному диалогу.", reply_markup=kb_markup)
                return

            if data == "kb:status":
                docs = _exec_scalar(db, "SELECT COUNT(*) FROM kb_documents WHERE is_active") or 0
                chunks = _exec_scalar(db, "SELECT COUNT(*) FROM kb_chunks") or 0
                await q.edit_message_text(f"Документов: {docs}\nЧанков: {chunks}")
                return

            if data == "kb:sync":
                if not _is_admin(tg_id):
                    await q.edit_message_text("Доступ ограничён.")
                else:
                    await q.edit_message_text("Синхронизация запланирована (заглушка).")
                return

            if data == "kb:nop":
                return
    except Exception:
        log.exception("kb_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка обработчика /kb. Попробуйте ещё раз.")
        except Exception:
            pass

# ---------- service ----------
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Контекст текущего диалога очищен.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if hasattr(update, "message") and update.message:
            await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")
        elif hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.message.reply_text("⚠ Ошибка обработчика. Попробуйте ещё раз.")
    except Exception:
        pass

# ---------- build ----------
def build_app() -> Application:
    apply_migrations_if_needed()
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(CommandHandler("dialogs", dialogs))
    app.add_handler(CallbackQueryHandler(dialog_cb, pattern=r"^dlg:"))

    app.add_handler(CommandHandler("kb", kb))
    app.add_handler(CallbackQueryHandler(kb_cb, pattern=r"^kb:"))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_router))

    return app
