from __future__ import annotations
import tiktoken
import asyncio
from openai import OpenAI
from io import BytesIO
import os, re, inspect, tempfile, hashlib, sys, logging
from datetime import datetime
from urllib.parse import urlparse
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
try:
    from telegram import BufferedInputFile, InputFile
    HAS_BUFFERED = True
except Exception:  # PTB version without BufferedInputFile
    from telegram import InputFile  # type: ignore
    HAS_BUFFERED = False
from telegram.ext import ApplicationBuilder, Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from sqlalchemy import text as sa_text
from bot.settings import load_settings
from bot.db.session import SessionLocal

log = logging.getLogger(__name__)
settings = load_settings()
_oa_client = OpenAI(api_key=settings.openai_api_key)

# --- singleton lock for polling (ensures single instance) ---
_singleton_conn = None
def _ensure_single_instance() -> None:
    """
    Acquire a PostgreSQL advisory lock to ensure a single bot instance.
    If lock is already held, exit (to avoid multiple polling instances).
    """
    global _singleton_conn
    if _singleton_conn is not None:
        return
    dsn = getattr(settings, "database_url", None) or getattr(settings, "DATABASE_URL", None)
    if not dsn:
        log.warning("DATABASE_URL not set — skipping singleton lock (risk of polling conflict).")
        return
    lock_key = int(hashlib.sha1(f"{dsn}|{settings.telegram_bot_token}".encode("utf-8")).hexdigest()[:15], 16) % (2**31)
    try:
        _singleton_conn = psycopg2.connect(dsn)
        _singleton_conn.autocommit = True
        with _singleton_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            ok = cur.fetchone()[0]
        if not ok:
            log.error("‼️ Detected another bot instance (advisory lock busy). Exiting.")
            sys.exit(0)
        log.info("✅ Singleton lock acquired (pg_advisory_lock).")
    except Exception:
        log.exception("Failed to acquire singleton lock. Polling conflict risk remains.")

# --- post_init for Application: remove webhook before polling ---
async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("✅ Webhook removed, pending updates dropped.")
    except Exception:
        log.exception("Failed to delete webhook")

# Aliases for commands
async def kb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /kb command."""
    return await kb(update, context)

async def dialog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /dialogs command."""
    return await dialogs(update, context)

# --- Apply migrations at startup if needed ---
def apply_migrations_if_needed(force: bool = False) -> None:
    """
    If tables are missing (or force=True), apply Alembic migrations.
    """
    try:
        from bot.db.session import engine
        need = True
        if not force:
            with engine.connect() as conn:
                exists = conn.execute(sa_text("SELECT to_regclass('public.users')")).scalar()
                need = not bool(exists)
        if need:
            log.info("Auto-migrate: applying Alembic migrations...")
            import time
            from alembic.config import Config
            from alembic import command
            cfg = Config("alembic.ini")
            os.environ["DATABASE_URL"] = settings.database_url
            command.upgrade(cfg, "head")
            log.info("Auto-migrate: done")
        else:
            log.info("Auto-migrate: tables already present")
    except Exception:
        log.exception("Auto-migrate failed")

# ---------- DB helper functions ----------
def _exec_scalar(db, sql: str, **params):
    return db.execute(sa_text(sql), params).scalar()

def _exec_all(db, sql: str, **params):
    return db.execute(sa_text(sql), params).all()

def _ensure_user(db, tg_id: int) -> int:
    uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg", tg=tg_id)
    if uid:
        return uid
    uid = _exec_scalar(
        db,
        "INSERT INTO users (tg_user_id, is_admin, is_allowed, lang) VALUES (:tg, FALSE, TRUE, 'ru') RETURNING id",
        tg=tg_id
    )
    db.commit()
    return uid

def _ensure_dialog(db, user_id: int) -> int:
    did = _exec_scalar(
        db,
        "SELECT id FROM dialogs WHERE user_id=:u AND is_deleted=FALSE ORDER BY created_at DESC LIMIT 1",
        u=user_id
    )
    if did:
        return did
    did = _exec_scalar(
        db,
        "INSERT INTO dialogs (user_id, title, style, model, is_deleted) VALUES (:u, :t, 'expert', :m, FALSE) RETURNING id",
        u=user_id, t=datetime.now().strftime("%Y-%m-%d | диалог"), m=settings.openai_model
    )
    db.commit()
    return did

def _create_new_dialog_for_tg(db, tg_id: int) -> int:
    uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg ORDER BY id LIMIT 1", tg=tg_id)
    if not uid:
        uid = _ensure_user(db, tg_id)
    today = datetime.now().date().isoformat()
    cnt = _exec_scalar(
        db,
        "SELECT count(*) FROM dialogs d JOIN users u ON u.id = d.user_id WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE",
        tg=tg_id
    ) or 0
    title = f"{today} | диалог {cnt+1}"
    did = _exec_scalar(
        db,
        "INSERT INTO dialogs (user_id, title, style, model, is_deleted, created_at, last_message_at) VALUES (:u, :t, :s, :m, FALSE, now(), NULL) RETURNING id",
        u=uid, t=title, s="pro", m=settings.openai_model
    )
    db.commit()
    return int(did)

def _is_admin(tg_id: int) -> bool:
    try:
        ids = [int(x.strip()) for x in (settings.admin_user_ids or "").split(",") if x.strip()]
        return tg_id in ids
    except Exception:
        return False

def _get_active_dialog_id(db, tg_id: int) -> int | None:
    row = db.execute(sa_text(
        "SELECT d.id FROM dialogs d JOIN users u ON u.id = d.user_id "
        "WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE "
        "ORDER BY COALESCE(d.last_message_at, to_timestamp(0)) DESC, COALESCE(d.created_at, to_timestamp(0)) DESC, d.id DESC "
        "LIMIT 1"
    ), {"tg": tg_id}).first()
    return row[0] if row else None

# --- Admin maintenance commands ---
async def repair_schema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Stepwise repair of DB schema (admins only).
    """
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")
        await m.reply_text("🧰 Ремонт схемы начат. Прогресс в логах...")
        created = []
        with SessionLocal() as db:
            def has(table: str) -> bool:
                return bool(db.execute(sa_text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar())
            # 0) vector extension
            try:
                db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
                db.commit()
                log.info("repair: extension vector OK (or already exists)")
            except Exception:
                db.rollback()
                log.exception("repair: CREATE EXTENSION vector failed (continuing without it)")
            # 1) users table
            if not has("users"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        tg_user_id BIGINT UNIQUE NOT NULL,
                        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                        is_allowed BOOLEAN NOT NULL DEFAULT TRUE,
                        lang TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """))
                db.commit(); created.append("users"); log.info("repair: created users")
            # 2) dialogs table
            if not has("dialogs"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS dialogs (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        title TEXT,
                        style VARCHAR(20) NOT NULL DEFAULT 'expert',
                        model TEXT,
                        is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_message_at TIMESTAMPTZ
                    )
                """))
                try:
                    db.execute(sa_text("""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dialogs_users'
                            ) THEN
                                ALTER TABLE dialogs
                                ADD CONSTRAINT fk_dialogs_users
                                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
                            END IF;
                        END $$;
                    """))
                except Exception:
                    log.exception("repair: FK dialogs->users skipped")
                db.commit(); created.append("dialogs"); log.info("repair: created dialogs")
            # 3) messages table
            if not has("messages"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id BIGSERIAL PRIMARY KEY,
                        dialog_id BIGINT NOT NULL,
                        role VARCHAR(20) NOT NULL,
                        content TEXT NOT NULL,
                        tokens INTEGER,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """))
                try:
                    db.execute(sa_text("""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_constraint WHERE conname = 'fk_messages_dialogs'
                            ) THEN
                                ALTER TABLE messages
                                ADD CONSTRAINT fk_messages_dialogs
                                FOREIGN KEY (dialog_id) REFERENCES dialogs(id) ON DELETE CASCADE;
                            END IF;
                        END $$;
                    """))
                except Exception:
                    log.exception("repair: FK messages->dialogs skipped")
                db.commit(); created.append("messages"); log.info("repair: created messages")
            # 4) kb_documents table
            try:
                if not has("kb_documents"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS kb_documents (
                            id BIGSERIAL PRIMARY KEY,
                            path TEXT UNIQUE NOT NULL,
                            etag TEXT,
                            mime TEXT,
                            pages INTEGER,
                            bytes BIGINT,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            is_active BOOLEAN NOT NULL DEFAULT TRUE
                        )
                    """))
                    db.commit(); created.append("kb_documents"); log.info("repair: created kb_documents")
            except Exception:
                db.rollback(); log.exception("repair: create kb_documents failed (skipped)")
            # 5) kb_chunks table
            try:
                if not has("kb_chunks"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS kb_chunks (
                            id BIGSERIAL PRIMARY KEY,
                            document_id BIGINT NOT NULL,
                            chunk_index INTEGER NOT NULL,
                            content TEXT NOT NULL,
                            meta JSON,
                            embedding vector(3072)
                        )
                    """))
                    try:
                        db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id)"))
                        db.execute(sa_text("CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops)"))
                    except Exception:
                        log.exception("repair: kb_chunks indexes skipped")
                    try:
                        db.execute(sa_text("""
                            DO $$
                            BEGIN
                                IF NOT EXISTS (
                                    SELECT 1 FROM pg_constraint WHERE conname = 'fk_kb_chunks_docs'
                                ) THEN
                                    ALTER TABLE kb_chunks
                                    ADD CONSTRAINT fk_kb_chunks_docs
                                    FOREIGN KEY (document_id) REFERENCES kb_documents(id) ON DELETE CASCADE;
                                END IF;
                            END $$;
                        """))
                    except Exception:
                        log.exception("repair: FK kb_chunks->kb_documents skipped")
                    db.commit(); created.append("kb_chunks"); log.info("repair: created kb_chunks")
            except Exception:
                db.rollback(); log.exception("repair: create kb_chunks failed (maybe missing vector extension)")
            # 6) dialog_kb_links table
            try:
                if not has("dialog_kb_links"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS dialog_kb_links (
                            id BIGSERIAL PRIMARY KEY,
                            dialog_id BIGINT NOT NULL,
                            document_id BIGINT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """))
                    db.commit(); created.append("dialog_kb_links"); log.info("repair: created dialog_kb_links")
            except Exception:
                db.rollback(); log.exception("repair: create dialog_kb_links failed")
            # 7) pdf_passwords table
            try:
                if not has("pdf_passwords"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS pdf_passwords (
                            id BIGSERIAL PRIMARY KEY,
                            dialog_id BIGINT NOT NULL,
                            document_id BIGINT NOT NULL,
                            pwd_hash TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """))
                    db.commit(); created.append("pdf_passwords"); log.info("repair: created pdf_passwords")
            except Exception:
                db.rollback(); log.exception("repair: create pdf_passwords failed")
            # 8) audit_log table
            try:
                if not has("audit_log"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS audit_log (
                            id BIGSERIAL PRIMARY KEY,
                            user_id BIGINT,
                            action TEXT,
                            meta JSON,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                    """))
                    db.commit(); created.append("audit_log"); log.info("repair: created audit_log")
            except Exception:
                db.rollback(); log.exception("repair: create audit_log failed")
        await m.reply_text("✅ Готово. Создано: " + (", ".join(created) if created else "ничего (всё уже было)"))
    except Exception:
        log.exception("repair_schema failed (outer)")
        await m.reply_text("⚠ Ошибка repair_schema. Смотри логи.")

async def dbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check existence of key tables in DB."""
    try:
        with SessionLocal() as db:
            rows = db.execute(sa_text("""
                select 'users' as t, to_regclass('public.users') is not null
                union all select 'dialogs', to_regclass('public.dialogs') is not null
                union all select 'messages', to_regclass('public.messages') is not null
                union all select 'kb_documents', to_regclass('public.kb_documents') is not null
                union all select 'kb_chunks', to_regclass('public.kb_chunks') is not null
                union all select 'dialog_kb_links', to_regclass('public.dialog_kb_links') is not null
                union all select 'pdf_passwords', to_regclass('public.pdf_passwords') is not null
                union all select 'audit_log', to_regclass('public.audit_log') is not null
            """)).all()
        lines = ["Проверка таблиц:"]
        for t, ok in rows:
            lines.append(f"{'✅' if ok else '❌'} {t}")
        await (update.effective_message or update.message).reply_text("\n".join(lines))
    except Exception:
        log.exception("dbcheck failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка dbcheck. Смотри логи.")

async def migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force run Alembic migrations (admins only)."""
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.effective_message or update.message).reply_text("Только для админа.")
        await (update.effective_message or update.message).reply_text("🔧 Запускаю миграции...")
        from alembic.config import Config
        from alembic import command
        os.environ["DATABASE_URL"] = settings.database_url
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        await (update.effective_message or update.message).reply_text("✅ Миграции применены.")
    except Exception:
        log.exception("migrate failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка миграции. Смотри логи.")

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check for DB connection."""
    try:
        with SessionLocal() as db:
            db.execute(sa_text("SELECT 1"))
        await update.message.reply_text("✅ OK: DB connection")
    except Exception:
        log.exception("health failed")
        await update.message.reply_text("❌ FAIL: DB connection")

# ---------- Bot command handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    await m.reply_text(
        "Здоров! Я помогу искать ответы в документах из БЗ и вести диалоги в разных стилях.\n"
        "Все команды тут — /help"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    await m.reply_text(
        "/start — приветствие\n"
        "/help — полный список команд\n"
        "/dialogs — список диалогов (открыть/переименовать/экспорт/удалить)\n"
        "/dialog_new — создать новый диалог\n"
        "/kb — подключить/отключить документы из БЗ\n"
        "/stats — карточка активного диалога\n"
        "/model — выбрать модель (ТОП-10 + Показать ещё)\n"
        "/mode — стиль ответа (pro/expert/user/ceo)\n"
        "/img <описание> — генерация изображения (покажу итоговый prompt)\n"
        "/web <запрос> — веб-поиск ответа\n"
        "/reset — сброс контекста активного диалога\n"
        "/whoami — мои права\n"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        tg = update.effective_user.id
        with SessionLocal() as db:
            # whoami info
            row = db.execute(sa_text(
                "SELECT id, is_admin, is_allowed, COALESCE(lang,'ru') FROM users WHERE tg_user_id=:tg ORDER BY id LIMIT 1"
            ), {"tg": tg}).first()
            if row:
                uid, is_admin, is_allowed, lang = row
            else:
                uid, is_admin, is_allowed, lang = (None, False, (False if getattr(settings, 'allowed_user_ids', '') else True), 'ru')
            role = "admin" if is_admin else ("allowed" if is_allowed or not getattr(settings, 'allowed_user_ids', '') else "guest")
            did = _get_active_dialog_id(db, tg)
            if not did:
                return await m.reply_text(
                    f"whoami: tg={tg}, role={role}, lang={lang}\n\nАктивного диалога нет. Создайте /dialog_new."
                )
            dialog_info = db.execute(sa_text(
                "SELECT title, model, style, created_at, last_message_at FROM dialogs WHERE id=:d"
            ), {"d": did}).first()
            links = db.execute(sa_text(
                "SELECT kd.path FROM dialog_kb_links l JOIN kb_documents kd ON kd.id = l.document_id "
                "WHERE l.dialog_id = :d ORDER BY kd.path"
            ), {"d": did}).fetchall()
            msg_count = _exec_scalar(db, "SELECT count(*) FROM messages WHERE dialog_id = :d", d=did) or 0
            total_dialogs = _exec_scalar(db,
                "SELECT count(*) FROM dialogs d JOIN users u ON u.id = d.user_id "
                "WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE",
                tg=tg
            ) or 0
        title, model, style, created_dt, updated_dt = dialog_info
        created = created_dt.strftime("%Y-%m-%d %H:%M") if created_dt else "-"
        updated = updated_dt.strftime("%Y-%m-%d %H:%M") if updated_dt else "-"
        docs = [r[0] for r in links] if links else []
        await m.reply_text("\n".join([
            f"whoami: tg={tg}, role={role}, lang={lang}",
            "",
            f"Диалог: {did} — {title or ''}",
            f"Модель: {model or settings.openai_model} | Стиль: {style or '-'}",
            f"Создан: {created} | Изменён: {updated}",
            f"Подключённые документы ({len(docs)}):",
            *([f"• {p}" for p in docs] or ["• —"]),
            "",
            f"Всего твоих диалогов: {total_dialogs} | Сообщений в этом диалоге: {msg_count}"
        ]))
    except Exception:
        log.exception("stats failed")
        await m.reply_text("⚠ Ошибка /stats")

async def dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        tg = update.effective_user.id
        with SessionLocal() as db:
            did = _create_new_dialog_for_tg(db, tg)
        await m.reply_text(f"✅ Создан диалог #{did}")
    except Exception:
        log.exception("dialog_new failed")
        await m.reply_text("⚠ Ошибка создания диалога")

async def img_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply_text("Использование: /img <описание>")
    prompt = parts[1]
    try:
        from bot.openai_helper import generate_image_bytes
        img_bytes, final_prompt = await generate_image_bytes(prompt)
        await m.reply_photo(photo=img_bytes, caption=f"🖼️ Сгенерировано DALL·E 3\nPrompt → {final_prompt}")
    except Exception:
        log.exception("img failed")
        await m.reply_text("⚠ Не удалось сгенерировать изображение.")

async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.effective_message or update.message).reply_text("⛔ Доступ запрещён (нужно быть админом).")
        args = (update.message.text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            return await (update.message or update.effective_message).reply_text("Использование: /grant <tg_id>")
        target = int(args[1])
        with SessionLocal() as db:
            uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg", tg=target)
            if not uid:
                uid = _exec_scalar(db,
                    "INSERT INTO users (tg_user_id, is_admin, is_allowed, lang) VALUES (:tg, FALSE, TRUE, 'ru') RETURNING id",
                    tg=target
                )
            else:
                db.execute(sa_text("UPDATE users SET is_allowed=TRUE WHERE id=:id"), {"id": uid})
            db.commit()
        await (update.message or update.effective_message).reply_text(f"✅ Выдан доступ пользователю {target}")
    except Exception:
        log.exception("grant failed")
        await (update.message or update.effective_message).reply_text("⚠ Ошибка grant")

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.message or update.effective_message).reply_text("⛔ Доступ запрещён (нужно быть админом).")
        args = (update.message.text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            return await (update.message or update.effective_message).reply_text("Использование: /revoke <tg_id>")
        target = int(args[1])
        with SessionLocal() as db:
            uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg", tg=target)
            if uid:
                db.execute(sa_text("UPDATE users SET is_allowed=FALSE WHERE id=:id"), {"id": uid})
                db.commit()
        await (update.message or update.effective_message).reply_text(f"🚫 Доступ отозван у {target}")
    except Exception:
        log.exception("revoke failed")
        await (update.message or update.effective_message).reply_text("⚠ Ошибка revoke")

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = update.effective_user.id
        with SessionLocal() as db:
            row = db.execute(sa_text("SELECT id, is_admin, is_allowed, lang FROM users WHERE tg_user_id=:tg"), {"tg": tg_id}).first()
            if not row:
                uid = _ensure_user(db, tg_id)
                row = db.execute(sa_text("SELECT id, is_admin, is_allowed, lang FROM users WHERE id=:id"), {"id": uid}).first()
        is_admin = bool(row[1]) if row else False
        is_allowed = bool(row[2]) if row else True
        lang = (row[3] or "ru") if row else "ru"
        role = "admin" if is_admin else ("allowed" if is_allowed else "guest")
        await (update.message or update.effective_message).reply_text(f"whoami: tg={tg_id}, role={role}, lang={lang}")
    except Exception:
        log.exception("whoami failed")
        await (update.message or update.effective_message).reply_text("⚠ Ошибка whoami")

async def rag_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    query = " ".join(context.args) if context.args else ""
    if not query:
        return await m.reply_text("Напишите запрос: /rag_diag ваш вопрос")
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            rows = _retrieve_chunks(db, did, query, k=5)
            if not rows:
                return await m.reply_text("Ничего релевантного не нашли среди подключённых документов.")
            out = []
            for i, r in enumerate(rows, 1):
                path = (r.get("path") or (r.get("meta") or {}).get("path", "")).split("/")[-1]
                sample = (r.get("content") or "")[:140].replace("\n", " ")
                out.append(f"[{i}] {path} — “{sample}…”")
            await m.reply_text("\n".join(out))
    except Exception:
        log.exception("rag_diag failed")
        await m.reply_text("⚠ rag_diag: ошибка. Смотри логи.")

async def rag_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            t = db.execute(sa_text("SELECT pg_typeof(embedding)::text FROM kb_chunks LIMIT 1")).scalar()
            d = db.execute(sa_text("SELECT (embedding <=> embedding) FROM kb_chunks LIMIT 1")).scalar()
        await m.reply_text(f"pg_typeof(embedding) = {t}\n(embedding <=> embedding) = {d}")
    except Exception as e:
        log.exception("rag_selftest failed")
        await m.reply_text(f"❌ rag_selftest: {e}")

async def kb_pdf_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")
        root = settings.yandex_root_path
        files = [f for f in _ya_list_files(root) if (f.get("name") or "").lower().endswith(".pdf")]
        lines = []
        for it in files:
            path = it.get("path") or it.get("name")
            try:
                blob = ya_download(path)
                txt, pages, is_prot = _pdf_extract_text(blob)
                sample = (txt or "").strip().replace("\n", " ")
                sample = (sample[:120] + "…") if len(sample) > 120 else sample
                lines.append(f"• {path.split('/')[-1]} | pages={pages} | prot={'yes' if is_prot else 'no'} | text_len={len(txt or '')} | sample='{sample}'")
            except Exception as e:
                lines.append(f"• {path.split('/')[-1]} | ERROR: {e}")
        if not lines:
            lines = ["(PDF не найдены)"]
        await m.reply_text("PDF DIAG:\n" + "\n".join(lines[:30]))
    except Exception:
        log.exception("kb_pdf_diag failed")
        await m.reply_text("⚠ kb_pdf_diag: ошибка. Смотри логи.")

async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    text = (m.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await m.reply_text("Использование: /web <запрос>")
    query = parts[1].strip()
    note = await m.reply_text("🔎 Ищу в вебе, подожди пару секунд…")
    try:
        from bot.web_search import web_search_digest, sources_footer
        answer, sources = await web_search_digest(query, max_results=6, openai_api_key=settings.openai_api_key)
        footer = ("\n\nИсточники:\n" + sources_footer(sources)) if sources else ""
        await _send_long(m, (answer or "Готово.") + footer)
        if sources:
            buttons = [[InlineKeyboardButton(f"[{i+1}] {urlparse(s['url']).netloc}", url=s['url'])] for i, s in enumerate(sources)]
            await m.reply_text("Открыть источники:", reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
    except Exception as e:
        await m.reply_text(f"⚠ Веб-поиск недоступен: {e}")
    finally:
        try:
            await note.delete()
        except Exception:
            pass

async def kb_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("⛔ Доступ только админам.")
        with SessionLocal() as db:
            total_docs = _exec_scalar(db, "SELECT count(*) FROM kb_documents") or 0
            total_chunks = _exec_scalar(db, "SELECT count(*) FROM kb_chunks") or 0
            orphan_chunks = _exec_scalar(db,
                "SELECT count(*) FROM kb_chunks c LEFT JOIN kb_documents d ON c.document_id = d.id WHERE d.id IS NULL"
            ) or 0
            dangle_links = _exec_scalar(db,
                "SELECT count(*) FROM dialog_kb_links l LEFT JOIN kb_documents d ON l.document_id = d.id WHERE d.id IS NULL"
            ) or 0
            inactive_links = _exec_scalar(db,
                "SELECT count(*) FROM dialog_kb_links l JOIN kb_documents d ON l.document_id = d.id WHERE d.is_active = FALSE"
            ) or 0
        lines = [
            f"Документов: {total_docs}",
            f"Чанков: {total_chunks}"
        ]
        if orphan_chunks:
            lines.append(f"⚠ Orphan chunks: {orphan_chunks}")
        if dangle_links:
            lines.append(f"⚠ Dangle links: {dangle_links}")
        if inactive_links:
            lines.append(f"⚠ Links to inactive docs: {inactive_links}")
        await m.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("kb_diag failed")
        await m.reply_text(f"⚠ Ошибка /kb_diag: {e}")

# ---- Callback query handlers ----
async def model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data in ("model:close", "model:nop"):
            try:
                await q.delete_message()
            except Exception:
                pass
            return
        if data.startswith("model:more:"):
            page = int(data.split(":")[-1])
            ids = context.user_data.get("all_models_sorted") or []
            return await q.edit_message_reply_markup(reply_markup=_page_models(ids, page))
        if data.startswith("model:set:"):
            mid = data.split(":", 2)[-1]
            try:
                client = OpenAI(api_key=settings.openai_api_key)
                client.chat.completions.create(model=mid, messages=[{"role": "user", "content": "ping"}], max_tokens=1)
            except Exception:
                return await q.edit_message_text(f"❌ Не удалось выбрать модель «{mid}». Попробуйте другую.")
            tg = update.effective_user.id
            with SessionLocal() as db:
                did = _exec_scalar(db,
                    "SELECT d.id FROM dialogs d JOIN users u ON u.id = d.user_id "
                    "WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE ORDER BY d.created_at DESC LIMIT 1",
                    tg=tg
                )
                if not did:
                    return await q.edit_message_text("Нет активного диалога. Нажми /dialog_new.")
                db.execute(sa_text("UPDATE dialogs SET model=:m WHERE id=:d"), {"m": mid, "d": did})
                db.commit()
            return await q.edit_message_text(f"✅ Установлена модель: {mid}")
    except Exception:
        log.exception("model_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка выбора модели")
        except Exception:
            pass

async def mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data == "mode:close":
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if data.startswith("mode:set:"):
        style = data.split(":", 2)[2]
        if style not in ("ceo", "expert", "pro", "user"):
            return await q.message.reply_text("Недопустимый стиль.")
        with SessionLocal() as db:
            uid = _ensure_user(db, q.from_user.id)
            did = _ensure_dialog(db, uid)
            db.execute(sa_text("UPDATE dialogs SET style=:s WHERE id=:d"), {"s": style, "d": did})
            db.commit()
        sample = _STYLE_EXAMPLES.get(style, "")
        await q.message.reply_text(f"✅ Установлен стиль: {style}\nПример: {sample}")
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        tg = update.effective_user.id
        if data == "dlg:nop":
            return
        if data == "dlg:new":
            with SessionLocal() as db:
                did = _create_new_dialog_for_tg(db, tg)
            return await q.edit_message_text(f"✅ Создан диалог #{did}")
        if data.startswith("dlg:page:"):
            page = int(data.split(":")[-1])
            with SessionLocal() as db:
                ds = _exec_all(db,
                    "SELECT d.id, COALESCE(d.title,'') FROM dialogs d JOIN users u ON u.id = d.user_id "
                    "WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE ORDER BY d.created_at DESC",
                    tg=tg
                )
            total = len(ds)
            DIALOGS_PAGE_SIZE = 6
            pages = max(1, (total + DIALOGS_PAGE_SIZE - 1) // DIALOGS_PAGE_SIZE)
            page = max(1, min(page, pages))
            beg = (page - 1) * DIALOGS_PAGE_SIZE
            chunk = ds[beg:beg + DIALOGS_PAGE_SIZE]
            rows = []
            for did, title in chunk:
                name = title or f"Диалог #{did}"
                rows.append([
                    InlineKeyboardButton(name[:30] + ("…" if len(name) > 30 else ""), callback_data=f"dlg:open:{did}"),
                    InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{did}"),
                    InlineKeyboardButton("📤", callback_data=f"dlg:export:{did}"),
                    InlineKeyboardButton("🗑️", callback_data=f"dlg:delete:{did}")
                ])
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("« Назад", callback_data=f"dlg:page:{page-1}"))
            nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="dlg:nop"))
            if page < pages:
                nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"dlg:page:{page+1}"))
            rows.append(nav)
            rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")])
            return await q.edit_message_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(rows))
        if data.startswith("dlg:open:"):
            dlg_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                db.execute(sa_text("UPDATE dialogs SET last_message_at = now() WHERE id = :d"), {"d": dlg_id})
                db.commit()
            return await q.edit_message_text(f"Открыт диалог #{dlg_id}")
        if data.startswith("dlg:rename:"):
            dlg_id = int(data.split(":")[-1])
            context.user_data["rename_dialog_id"] = dlg_id
            return await q.edit_message_text("Введите новое название диалога:")
        if data.startswith("dlg:export:"):
            dlg_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                msgs = _exec_all(
                    db,
                    "SELECT role, content, created_at FROM messages WHERE dialog_id=:d ORDER BY created_at",
                    d=dlg_id
                )
            lines = ["# Экспорт диалога", ""]
            for role, content, _ in msgs:
                who = "Пользователь" if role == "user" else "Бот"
                lines.append(f"**{who}:**\n{content}\n")
            data_bytes = "\n".join(lines).encode("utf-8")
            file = BufferedInputFile(data_bytes, filename=f"dialog_{dlg_id}.md") if HAS_BUFFERED else InputFile(data_bytes, filename=f"dialog_{dlg_id}.md")
            await q.message.reply_document(document=file, caption="Экспорт готов")
            return
        if data.startswith("dlg:delete:"):
            dlg_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                db.execute(sa_text("UPDATE dialogs SET is_deleted = TRUE WHERE id = :d"), {"d": dlg_id})
                db.commit()
            return await q.edit_message_text(f"Диалог #{dlg_id} удалён")
    except Exception:
        log.exception("dialog_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка обработчика /dialogs. Попробуйте ещё раз.")
        except Exception:
            pass

async def kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        mode = context.user_data.get("kb_mode", "all")
        page = context.user_data.get("kb_page", 1)
        with SessionLocal() as db:
            text, markup, pages, page, _ = _kb_build_ui(db, update.effective_user.id, mode, page)
        context.user_data["kb_page"] = page
        await m.reply_text(text, reply_markup=markup)
    except Exception:
        log.exception("kb failed")
        await m.reply_text("⚠ Ошибка /kb")

async def kb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await q.answer()
    try:
        mode = context.user_data.get("kb_mode", "all")
        page = context.user_data.get("kb_page", 1)
        if data == "kb:nop":
            return
        if data.startswith("kb:page:"):
            page = int(data.split(":")[-1])
            context.user_data["kb_page"] = page
            with SessionLocal() as db:
                text, markup, _, page, _ = _kb_build_ui(db, update.effective_user.id, mode, page)
            return await q.edit_message_text(text, reply_markup=markup)
        if data.startswith("kb:mode:"):
            mode = data.split(":")[-1]
            context.user_data["kb_mode"] = mode
            context.user_data["kb_page"] = 1
            with SessionLocal() as db:
                text, markup, _, page, _ = _kb_build_ui(db, update.effective_user.id, mode, 1)
            return await q.edit_message_text(text, reply_markup=markup)
        if data.startswith("kb:toggle:"):
            doc_id = int(data.split(":")[-1])
            with SessionLocal() as db:
                did = _get_active_dialog_id(db, update.effective_user.id) or _create_new_dialog_for_tg(db, update.effective_user.id)
                exists = _exec_scalar(db,
                    "SELECT 1 FROM dialog_kb_links WHERE dialog_id=:d AND document_id=:doc LIMIT 1",
                    d=did, doc=doc_id
                )
                if exists:
                    db.execute(sa_text("DELETE FROM dialog_kb_links WHERE dialog_id=:d AND document_id=:doc"), {"d": did, "doc": doc_id})
                else:
                    db.execute(sa_text(
                        "INSERT INTO dialog_kb_links (dialog_id, document_id, created_at) VALUES (:d,:doc,now()) ON CONFLICT DO NOTHING"
                    ), {"d": did, "doc": doc_id})
                db.commit()
                text, markup, _, page, _ = _kb_build_ui(db, update.effective_user.id, mode, page)
            return await q.edit_message_text(text, reply_markup=markup)
        if data in ("kb:sync", "kb:sync:run"):
            return await kb_sync_admin(update, context)
        if data == "kb:status":
            with SessionLocal() as db:
                d = _exec_scalar(db, "SELECT count(*) FROM kb_documents") or 0
                c = _exec_scalar(db, "SELECT count(*) FROM kb_chunks") or 0
            return await q.message.reply_text(f"БЗ: документов {d}, чанков {c}")
    except Exception:
        log.exception("kb_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка меню БЗ.")
        except Exception:
            pass

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            did = _get_active_dialog_id(db, update.effective_user.id)
            if not did:
                return await m.reply_text("Нет активного диалога.")
            db.execute(sa_text("DELETE FROM messages WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("DELETE FROM dialog_kb_links WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("DELETE FROM pdf_passwords WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("UPDATE dialogs SET last_message_at=NULL WHERE id=:d"), {"d": did})
            db.commit()
        context.user_data.clear()
        await m.reply_text("♻️ Диалог очищен: история, привязки БЗ и пароли PDF сброшены.")
    except Exception:
        log.exception("reset failed")
        await m.reply_text("⚠ Не удалось сбросить диалог.")

# ---- Model/Mode selection helpers ----
def _model_score(mid: str) -> int:
    m = mid.lower()
    if any(x in m for x in ["dall-e", "whisper", "embedding", "text-embedding", "tts", "audio"]):
        return -100
    score = 0
    if "latest" in m: score += 10
    if "preview" in m: score += 6
    if any(x in m for x in ["o4", "4o"]): score += 100
    if "4.1" in m: score += 80
    if "4" in m: score += 60
    if "mini" in m: score += 8
    if "turbo" in m: score += 4
    if "3.5" in m: score += 1
    return score

def _keep_chat_model(mid: str) -> bool:
    m = mid.lower()
    if any(x in m for x in ["embedding", "text-embedding", "dall-e", "whisper", "tts", "audio", "moderation", "computer-use"]):
        return False
    if m.startswith("babbage") or m.startswith("davinci") or m.startswith("curie") or m.startswith("ada"):
        return False
    if m.startswith("gpt-5"):
        return False
    return any(x in m for x in ["gpt-4", "gpt-3.5", "chatgpt-4o", "o4"])

def _sort_models(models):
    def score(item):
        mid = getattr(item, "id", "")
        created = getattr(item, "created", 0) or 0
        return (created, len(mid) * -1)
    return sorted(models, key=score, reverse=True)

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        res = client.models.list()
        models = [it for it in getattr(res, "data", []) if _keep_chat_model(getattr(it, "id", ""))]
        models = _sort_models(models)
        ids = [it.id for it in models]
        context.user_data["all_models_sorted"] = ids
        top10 = ids[:10]
        buttons = [[InlineKeyboardButton(mid, callback_data=f"model:set:{mid}")] for mid in top10]
        if len(ids) > 10:
            buttons.append([InlineKeyboardButton("Показать ещё", callback_data="model:more:2")])
        buttons.append([InlineKeyboardButton("Закрыть", callback_data="model:close")])
        await m.reply_text("Выберите модель для текущего диалога:", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        log.exception("model_menu failed")
        await m.reply_text("⚠ Не удалось получить список моделей")

def _page_models(ids: list[str], page: int, page_size: int = 10):
    pages = max(1, (len(ids) + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    beg = (page - 1) * page_size
    chunk = ids[beg:beg + page_size]
    rows = [[InlineKeyboardButton(mid, callback_data=f"model:set:{mid}")] for mid in chunk]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"model:more:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="model:nop"))
    if page < pages:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"model:more:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("Закрыть", callback_data="model:close")])
    return InlineKeyboardMarkup(rows)

async def mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Профессионал", callback_data="mode:set:pro")],
        [InlineKeyboardButton("Эксперт", callback_data="mode:set:expert")],
        [InlineKeyboardButton("Пользователь", callback_data="mode:set:user")],
        [InlineKeyboardButton("СЕО", callback_data="mode:set:ceo")],
        [InlineKeyboardButton("Закрыть", callback_data="mode:close")]
    ])
    await m.reply_text("Выберите стиль ответа для текущего диалога:", reply_markup=kb)

# ---- Knowledge Base UI helpers ----
KB_PAGE_SIZE = 10

def _kb_build_ui(db, tg_id: int, mode: str, page: int):
    """Возвращает (text, markup, total_pages, page, linked_ids_set)."""
    did = _get_active_dialog_id(db, tg_id) or _create_new_dialog_for_tg(db, tg_id)
    linked = set(x[0] for x in db.execute(sa_text(
        "SELECT document_id FROM dialog_kb_links WHERE dialog_id=:d"
    ), {"d": did}).fetchall())
    docs = db.execute(sa_text("""
        SELECT id, path, is_active
        FROM kb_documents
        ORDER BY path
    """)).fetchall()
    def is_linked(doc_id): return doc_id in linked
    if mode == "linked":
        docs = [r for r in docs if is_linked(r[0])]
    elif mode == "avail":
        docs = [r for r in docs if not is_linked(r[0])]
    total = len(docs)
    pages = max(1, (total + KB_PAGE_SIZE - 1) // KB_PAGE_SIZE)
    page = max(1, min(page, pages))
    beg = (page - 1) * KB_PAGE_SIZE
    chunk = docs[beg:beg + KB_PAGE_SIZE]
    rows = []
    for doc_id, path, is_active in chunk:
        check = "☑" if doc_id in linked else "☐"
        title = (path or f"doc #{doc_id}")[:70]
        rows.append([InlineKeyboardButton(f"{check} {title}", callback_data=f"kb:toggle:{doc_id}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("«", callback_data=f"kb:page:{page-1}"))
    nav.append(InlineKeyboardButton(f"Страница {page}/{pages}", callback_data="kb:nop"))
    if page < pages:
        nav.append(InlineKeyboardButton("»", callback_data=f"kb:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("Все", callback_data="kb:mode:all"),
        InlineKeyboardButton("Подключённые", callback_data="kb:mode:linked"),
        InlineKeyboardButton("Доступные", callback_data="kb:mode:avail")
    ])
    rows.append([InlineKeyboardButton("🗘 Синхронизация", callback_data="kb:sync")])
    rows.append([InlineKeyboardButton("📊 Статус БЗ", callback_data="kb:status")])
    text = "Меню БЗ: выберите документы для подключения к активному диалогу."
    return text, InlineKeyboardMarkup(rows), pages, page, linked

# ---- RAG and embedding helpers ----
def _vec_literal(vec: list[float]) -> tuple[dict, str]:
    arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
    return {"q": arr}, "CAST(:q AS vector)"

def _embed_query(text: str) -> list[float]:
    client = OpenAI(api_key=settings.openai_api_key)
    return client.embeddings.create(model=settings.openai_embedding_model, input=[text]).data[0].embedding

def _retrieve_chunks(db, dialog_id: int, question: str, k: int = 6) -> list[dict]:
    kind = _kb_embedding_column_kind(db)
    if kind != "vector":
        return []
    query_vec = _embed_query(question)
    params, qexpr = _vec_literal(query_vec)
    sql = f"""
        SELECT c.content, c.meta, d.path
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id AND d.is_active = TRUE
        JOIN dialog_kb_links l ON l.document_id = c.document_id
        WHERE l.dialog_id = :did
        ORDER BY c.embedding <=> {qexpr}
        LIMIT :k
    """
    p = {"did": dialog_id, "k": k}
    p.update(params)
    rows = db.execute(sa_text(sql), p).mappings().all()
    return [dict(r) for r in rows]

_STYLE_EXAMPLES = {
    "pro":    "Кратко, по шагам, чек-лист. Без воды. Пример: «Шаги 1–5, риски, KPI, дедлайны».",
    "expert": "Глубоко и обстоятельно: причины/следствия, альтернативы, ссылки. Пример: «Начнём с контекста и ограничений…».",
    "user":   "Просто, понятным языком, с метафорами и примерами из жизни.",
    "ceo":    "С точки зрения бизнеса: ценность/стоимость, риски, сроки, решения, варианты и trade-offs."
}

def _build_prompt_with_style(ctx_blocks: list[str], user_q: str, dialog_style: str) -> str:
    style_map = {
        "pro":   "Профессионал: максимально ёмко и по делу, шаги и чек-лист.",
        "expert":"Эксперт: подробно, причины/следствия, альтернативы, выводы. Цитаты из источников только в конце.",
        "user":  "Пользователь: простыми словами, примеры и аналогии.",
        "ceo":   "CEO: бизнес-ценность, ROI, риски, решения и компромиссы."
    }
    style_line = style_map.get(dialog_style or "pro", style_map["pro"])
    header = (
        "Ты — аккуратный ассистент. Используй контекст БЗ, но не ограничивайся цитатами: "
        "синтезируй цельный ответ в выбранном стиле. Если уверенности нет — уточни."
    )
    ctx = "\n\n".join([f"[Фрагмент #{i+1}]\n{t}" for i, t in enumerate(ctx_blocks)])
    return f"{header}\nСтиль: {style_line}\n\nКонтекст:\n{ctx}\n\nВопрос: {user_q}"

def _format_citations(chunks: list[dict]) -> str:
    def short(p: str) -> str:
        return (p or "").split("/")[-1].split("?")[0]
    uniq = []
    for r in chunks:
        name = short(r.get("path") or (r.get("meta") or {}).get("path", ""))
        if name and name not in uniq:
            uniq.append(name)
    if not uniq:
        return ""
    return "\n\nИсточники: " + "; ".join(f"[{i+1}] {n}" for i, n in enumerate(uniq[:5]))

def _kb_embedding_column_kind(db) -> str:
    try:
        t = db.execute(sa_text("SELECT pg_typeof(embedding)::text FROM kb_chunks LIMIT 1")).scalar()
        if t:
            t = str(t).lower()
            if "vector" in t:
                return "vector"
            if "bytea" in t:
                return "bytea"
        t = db.execute(sa_text("""
            SELECT COALESCE(udt_name, data_type)
            FROM information_schema.columns
            WHERE table_name='kb_chunks' AND column_name='embedding'
            LIMIT 1
        """)).scalar()
        t = (t or "").lower()
        if "vector" in t:
            return "vector"
        if "bytea" in t:
            return "bytea"
    except Exception:
        pass
    return "none"

def _kb_upsert_document(db, path: str, mime: str, size: int, etag: str) -> int:
    sql = sa_text("""
        INSERT INTO kb_documents (path, mime, bytes, etag, updated_at, is_active)
        VALUES (:p, :m, :b, :e, now(), TRUE)
        ON CONFLICT (path) DO UPDATE
          SET mime = EXCLUDED.mime,
              bytes = EXCLUDED.bytes,
              etag  = EXCLUDED.etag,
              updated_at = now(),
              is_active = TRUE
        RETURNING id
    """)
    doc_id = db.execute(sql, {"p": path, "m": mime, "b": size, "e": etag}).scalar()
    db.commit()
    return int(doc_id)

def _kb_clear_chunks(db, document_id: int):
    db.execute(sa_text("DELETE FROM kb_chunks WHERE document_id=:d"), {"d": document_id})
    db.commit()

def _get_embeddings(chunks: list[str]) -> list[list[float]]:
    """
    Calculate embeddings in batches, handling token limits.
    """
    if not chunks:
        return []
    enc = tiktoken.get_encoding("cl100k_base")
    client = OpenAI(api_key=settings.openai_api_key)
    MAX_TOKENS_PER_REQ = 250_000
    MAX_ITEMS_PER_REQ  = 128
    out: list[list[float]] = []
    batch: list[str] = []
    batch_tok_sum = 0
    def flush_batch():
        nonlocal out, batch, batch_tok_sum
        if not batch:
            return
        resp = client.embeddings.create(model=settings.openai_embedding_model, input=batch)
        data = getattr(resp, "data", None) or resp.get("data", [])
        out.extend([item.embedding for item in data])
        batch = []
        batch_tok_sum = 0
    for ch in chunks:
        t = len(enc.encode(ch or ""))
        if t > MAX_TOKENS_PER_REQ:
            toks = enc.encode(ch or "")
            subchunks = [enc.decode(toks[i:i+2000]) for i in range(0, len(toks), 2000)]
            out.extend(_get_embeddings(subchunks))
            continue
        if batch and (batch_tok_sum + t > MAX_TOKENS_PER_REQ or len(batch) >= MAX_ITEMS_PER_REQ):
            flush_batch()
        batch.append(ch)
        batch_tok_sum += t
    flush_batch()
    return out

def _format_vector_sql(vec: list[float]) -> tuple[str, dict]:
    arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
    return " CAST(:emb AS vector) ", {"emb": arr}

def _format_bytea_sql(vec: list[float]) -> tuple[str, dict]:
    try:
        import struct
        from psycopg2 import Binary
        b = struct.pack(f"{len(vec)}f", *vec) if vec else b""
        return " :emb ", {"emb": Binary(b)}
    except Exception:
        return " NULL ", {}

def _ya_list_files(root_path: str):
    """
    Возвращает список файлов в папке Я.Диска через REST API.
    Элементы: name, path, type, mime_type, size, md5.
    """
    import requests
    YA_API = "https://cloud-api.yandex.net/v1/disk"
    out = []
    limit, offset = 200, 0
    headers = {"Authorization": f"OAuth {settings.yandex_disk_token}"}
    while True:
        params = {
            "path": root_path,
            "limit": limit,
            "offset": offset,
            "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.mime_type,_embedded.items.size,_embedded.items.md5"
        }
        r = requests.get(f"{YA_API}/resources", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = (data.get("_embedded") or {}).get("items") or []
        for it in items:
            if it.get("type") == "file":
                out.append(it)
        if len(items) < limit:
            break
        offset += limit
    return out

def _kb_update_pages(db, document_id: int, pages: int | None):
    if pages is None:
        return
    db.execute(sa_text("UPDATE kb_documents SET pages=:p, updated_at=now() WHERE id=:id"), {"p": pages, "id": document_id})
    db.commit()

async def kb_sync_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")
        await m.reply_text("📄 Индексация PDF началась…")
        root = settings.yandex_root_path
        files = [f for f in _ya_list_files(root) if (f.get("name") or "").lower().endswith(".pdf")]
        touched_docs = len(files)
        indexed_docs = 0
        indexed_chunks = 0
        with SessionLocal() as db:
            emb_kind = _kb_embedding_column_kind(db)
            present_paths = { (f.get("path") or f.get("name")) for f in files if (f.get("path") or f.get("name")) }
            rows = db.execute(sa_text("SELECT id, path, is_active FROM kb_documents WHERE mime LIKE 'application/pdf%'")).mappings().all()
            for r in rows:
                if r["path"] not in present_paths and r["is_active"]:
                    db.execute(sa_text("UPDATE kb_documents SET is_active=FALSE, updated_at=now() WHERE id=:id"), {"id": r["id"]})
            db.commit()
            for it in files:
                path = it.get("path") or it.get("name")
                mime = it.get("mime_type") or "application/pdf"
                size = int(it.get("size") or 0)
                etag = it.get("md5") or ""
                if not path:
                    continue
                doc_id = _kb_upsert_document(db, path=path, mime=mime, size=size, etag=etag)
                try:
                    blob = ya_download(path)
                except Exception as e:
                    log.exception("pdf download failed: %s (%s)", path, e)
                    continue
                try:
                    txt, pages, is_prot = _pdf_extract_text(blob)
                except Exception as e:
                    log.exception("pdf parse failed: %s (%s)", path, e)
                    continue
                _kb_update_pages(db, doc_id, pages if pages else None)
                if is_prot or not txt.strip():
                    log.info("pdf skipped (protected or empty): %s", path)
                    continue
                _kb_clear_chunks(db, doc_id)
                chunks = _chunk_text(txt, settings.chunk_size if hasattr(settings, 'chunk_size') else 1200, settings.chunk_overlap if hasattr(settings, 'chunk_overlap') else 200)
                embs = _get_embeddings(chunks) if emb_kind in ("vector", "bytea") else [[] for _ in chunks]
                doc_failed = False
                inserted = 0
                for idx, (ch, ve) in enumerate(zip(chunks, embs)):
                    meta = {"path": path, "mime": mime}
                    if emb_kind == "vector":
                        place, params = _format_vector_sql(ve)
                    elif emb_kind == "bytea":
                        place, params = _format_bytea_sql(ve)
                    else:
                        place, params = " NULL ", {}
                    sql = f"""
                        INSERT INTO kb_chunks (document_id, chunk_index, content, meta, embedding)
                        VALUES (:d, :i, :c, :meta, {place})
                    """
                    p = {"d": doc_id, "i": idx, "c": ch, "meta": json.dumps(meta)}
                    p.update(params)
                    try:
                        db.execute(sa_text(sql), p)
                        inserted += 1
                        if inserted % 200 == 0:
                            db.commit()
                    except Exception as e:
                        log.exception("insert pdf chunk failed (doc_id=%s, idx=%s): %s", doc_id, idx, e)
                        doc_failed = True
                        continue
                db.commit()
                if not doc_failed and inserted > 0:
                    indexed_docs += 1
                    indexed_chunks += inserted
        await m.reply_text(
            "✅ Индексация PDF завершена.\n"
            f"Документов учтено: {touched_docs}\n"
            f"Проиндексировано: {indexed_docs} документов, {indexed_chunks} чанков"
        )
    except Exception as e:
        log.exception("kb_sync_pdf failed")
        await (update.effective_message or update.message).reply_text(f"⚠ kb_sync_pdf: {e}")

async def kb_sync_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-синк БЗ: надёжно вызывает entrypoint из bot.knowledge_base.indexer."""
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")
    await m.reply_text("🔄 Синхронизация запущена...")
    try:
        from bot.knowledge_base import indexer
        explicit = getattr(settings, "kb_sync_entrypoint", None) or os.getenv("KB_SYNC_ENTRYPOINT")
        fn = getattr(indexer, explicit, None) if explicit else None
        if not fn:
            for name in ("sync_kb","sync_all","sync_from_yandex","sync","run_sync","full_sync","reindex","index_all","ingest_all","ingest","main"):
                if hasattr(indexer, name) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name)
                    break
        if not fn:
            for name in dir(indexer):
                if name.startswith("_"):
                    continue
                if re.search(r"(sync|index|ingest)", name, re.I) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name)
                    break
        if not fn:
            raise RuntimeError("Не найден entrypoint в indexer.py. Укажите KB_SYNC_ENTRYPOINT или реализуйте sync_kb(session).")
        sig = inspect.signature(fn)
        kwargs, session_to_close = {}, None
        for p in sig.parameters.values():
            nm = p.name.lower()
            if nm in ("session","db","dbsession","conn","connection"):
                sess = SessionLocal(); kwargs[p.name] = sess; session_to_close = sess
            elif nm in ("sessionlocal","session_factory","factory","engine"):
                kwargs[p.name] = SessionLocal
            elif nm in ("settings","cfg","config","conf"):
                kwargs[p.name] = settings
            elif p.default is inspect._empty:
                kwargs[p.name] = None
        import inspect as _inspect
        if _inspect.iscoroutinefunction(fn):
            result = await fn(**kwargs)
        else:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: fn(**kwargs))
        if session_to_close is not None:
            try:
                session_to_close.close()
            except Exception:
                pass
        if isinstance(result, dict):
            upd = result.get("updated"); skp = result.get("skipped"); tot = result.get("total")
            msg = "✅ Синхронизация завершена."
            if upd is not None or skp is not None or tot is not None:
                msg += f" Обновлено: {upd or 0}, пропущено: {skp or 0}, всего файлов на диске: {tot or 0}."
            return await m.reply_text(msg)
        elif isinstance(result, (tuple, list)) and len(result) >= 2:
            return await m.reply_text(f"✅ Готово: документов {result[0]}, чанков {result[1]}")
        else:
            return await m.reply_text("✅ Синхронизация завершена.")
    except Exception as e:
        log.exception("kb_sync_admin failed")
        return await m.reply_text(f"⚠ Ошибка синхронизации: {e}")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовый запрос: активный диалог, RAG, сохранение истории, длинные ответы."""
    m = update.effective_message or update.message
    if "rename_dialog_id" in context.user_data:
        dlg_id = context.user_data.pop("rename_dialog_id")
        new_title = (m.text or "").strip()[:100]
        if not new_title:
            return await m.reply_text("Название пустое. Отменено.")
        try:
            with SessionLocal() as db:
                db.execute(sa_text("UPDATE dialogs SET title=:t WHERE id=:d"), {"t": new_title, "d": dlg_id})
                db.commit()
            return await m.reply_text("Название сохранено.")
        except Exception:
            log.exception("rename dialog title failed")
            return await m.reply_text("⚠ Не удалось сохранить название.")
    q = (m.text or "").strip()
    if not q:
        return
    try:
        with SessionLocal() as db:
            tg = update.effective_user.id
            did = _get_active_dialog_id(db, tg)
            if not did:
                did = _create_new_dialog_for_tg(db, tg)
            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            dia_model = row[0] if row and row[0] else settings.openai_model
            dia_style = row[1] if row and row[1] else "pro"
            # Retrieve relevant chunks with diversification
            topk = getattr(settings, "kb_top_k", None) or getattr(settings, "KB_TOP_K", None) or 6
            topk = max(int(topk), 6)
            pool_k = max(topk * 3, topk)
            chunks_all = _retrieve_chunks(db, did, q, k=pool_k)
            chunks = diversify_chunks(chunks_all, k=topk)
            ctx_blocks = [c.get("content", "")[:1000] for c in chunks] if chunks else []
        prompt = _build_prompt_with_style(ctx_blocks, q, dia_style) if ctx_blocks else q
        system = {"role": "system", "content": "RAG assistant"}
        user = {"role": "user", "content": prompt}
        answer = await _chat_full(dia_model, [system, user], temperature=0.3)
        if chunks:
            answer += _format_citations(chunks)
        try:
            with SessionLocal() as db:
                db.execute(sa_text(
                    "INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'user',:c,now())"
                ), {"d": did, "c": q})
                db.execute(sa_text(
                    "INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'assistant',:c,now())"
                ), {"d": did, "c": answer})
                db.execute(sa_text("UPDATE dialogs SET last_message_at=now() WHERE id=:d"), {"d": did})
                db.commit()
        except Exception:
            log.exception("save messages failed")
        await _send_long(m, answer)
    except Exception:
        log.exception("on_text failed")
        await m.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice message: transcribe and answer as text query."""
    m = update.effective_message or update.message
    try:
        voice = m.voice or m.audio
        if not voice:
            return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")
        file = await context.bot.get_file(voice.file_id)
        tmpdir = tempfile.mkdtemp(prefix="tg_voice_")
        ogg_path = os.path.join(tmpdir, "voice.ogg")
        await file.download_to_drive(ogg_path)
        client = OpenAI(api_key=settings.openai_api_key)
        model_name = getattr(settings, "openai_transcribe_model", None) or getattr(settings, "OPENAI_TRANSCRIBE_MODEL", None) or "whisper-1"
        with open(ogg_path, "rb") as f:
            tr = client.audio.transcriptions.create(model=model_name, file=f)
        text = (getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None) or "").strip()
        if not text:
            return await m.reply_text("⚠ Не удалось распознать речь. Скажите ещё раз, пожалуйста.")
        low = text.lower()
        if low.startswith(("нарисуй", "сгенерируй картинку", "создай изображение", "сделай картинку")):
            if ":" in text:
                prompt = text.split(":", 1)[1].strip()
            else:
                parts = text.split(maxsplit=1)
                prompt = parts[1].strip() if len(parts) > 1 else ""
            if not prompt:
                return await m.reply_text("Уточните, что рисовать: «Нарисуй: стиль, объект, детали».")
            try:
                from bot.openai_helper import generate_image_bytes
                img_bytes, final_prompt = await generate_image_bytes(prompt)
                await m.reply_photo(photo=img_bytes, caption=f"🖼️ Сгенерировано по голосовой команде\nPrompt → {final_prompt}")
                with SessionLocal() as db:
                    tg_id = update.effective_user.id
                    did = _get_active_dialog_id(db, tg_id) or _create_new_dialog_for_tg(db, tg_id)
                    db.execute(sa_text(
                        "INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'user',:c,now())"
                    ), {"d": did, "c": text})
                    db.execute(sa_text(
                        "INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'assistant',:c,now())"
                    ), {"d": did, "c": f"[image]\nPrompt → {final_prompt}"})
                    db.execute(sa_text("UPDATE dialogs SET last_message_at=now() WHERE id=:d"), {"d": did})
                    db.commit()
                return
            except Exception:
                log.exception("voice->image failed")
                return await m.reply_text("⚠ Не удалось сгенерировать изображение. Попробуйте ещё раз позже.")
        update.effective_message.text = text
        return await on_text(update, context)
    except Exception:
        log.exception("on_voice failed")
        await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")

def ya_download(path: str) -> bytes:
    """
    Download a file from Yandex Disk by absolute path.
    Returns binary content.
    """
    import requests
    YA_API = "https://cloud-api.yandex.net/v1/disk"
    headers = {"Authorization": f"OAuth {settings.yandex_disk_token}"}
    r = requests.get(f"{YA_API}/resources/download", headers=headers, params={"path": path}, timeout=60)
    r.raise_for_status()
    href = (r.json() or {}).get("href")
    if not href:
        raise RuntimeError("download href not returned by Yandex Disk")
    f = requests.get(href, timeout=300)
    f.raise_for_status()
    return f.content

async def _send_long(m, text: str):
    """Отправляет текст пачками, если он длиннее лимита Telegram."""
    for chunk in _split_for_tg(text):
        await m.reply_text(chunk)

def _split_for_tg(text: str, limit: int = 3500):
    """Делит длинный текст так, чтобы не рвать слова/абзацы."""
    parts, s = [], text.strip()
    while len(s) > limit:
        cut = s.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = s.rfind("\n", 0, limit)
        if cut == -1:
            cut = s.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(s[:cut].rstrip())
        s = s[cut:].lstrip()
    if s:
        parts.append(s)
    return parts

async def _chat_full(model: str, messages: list, temperature: float = 0.3, max_turns: int = 6):
    """
    Calls Chat Completions repeatedly if needed (finish_reason == 'length'), up to max_turns.
    Returns the full concatenated response.
    """
    hist = list(messages)
    full = ""
    turns = 0
    while turns < max_turns:
        turns += 1
        resp = _oa_client.chat.completions.create(
            model=model,
            messages=hist,
            temperature=temperature,
            max_tokens=1024
        )
        choice = resp.choices[0]
        piece = choice.message.content or ""
        full += piece
        finish = choice.finish_reason
        if finish != "length":
            break
        hist.append({"role": "assistant", "content": piece})
        hist.append({"role": "user", "content": "Продолжай с того места. Не повторяйся."})
    return full

def diversify_chunks(chunks, k):
    """Select up to k chunks, ensuring different documents are represented first."""
    if not chunks:
        return []
    def doc_id_of(ch):
        if hasattr(ch, "document_id"):
            return ch.document_id
        if isinstance(ch, dict):
            return ch.get("document_id") or (ch.get("meta") or {}).get("document_id")
        return None
    pool = chunks[: max(k*3, k)]
    picked, seen = [], set()
    for ch in pool:
        did = doc_id_of(ch)
        if did is not None and did not in seen:
            picked.append(ch); seen.add(did)
            if len(picked) >= k:
                return picked
    for ch in pool:
        if ch in picked:
            continue
        picked.append(ch)
        if len(picked) >= k:
            break
    return picked

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if hasattr(update, "message") and update.message:
            await update.message.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")
        elif hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.message.reply_text("⚠ Ошибка обработчика. Попробуйте ещё раз.")
    except Exception:
        pass

def build_app() -> Application:
    apply_migrations_if_needed()
    _ensure_single_instance()
    app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(_post_init).build()
    app.add_error_handler(error_handler)
    app.add_handler(CallbackQueryHandler(model_cb, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(mode_cb, pattern=r"^mode:"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("model", model_menu))
    app.add_handler(CommandHandler("mode", mode_menu))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("dialogs", dialogs))
    app.add_handler(CommandHandler("dialog", dialog_cmd))
    app.add_handler(CommandHandler("dialog_new", dialog_new))
    app.add_handler(CommandHandler("kb", kb_cmd))
    app.add_handler(CommandHandler("kb_diag", kb_diag))
    app.add_handler(CommandHandler("img", img_cmd))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("rag_diag", rag_diag))
    app.add_handler(CommandHandler("rag_selftest", rag_selftest))
    app.add_handler(CommandHandler("kb_pdf_diag", kb_pdf_diag))
    app.add_handler(CommandHandler("web", web_cmd))
    app.add_handler(CommandHandler("kb_chunks", kb_chunks_cmd))
    app.add_handler(CommandHandler("kb_reindex", kb_reindex))
    app.add_handler(CommandHandler("kb_sync", kb_sync_admin))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
