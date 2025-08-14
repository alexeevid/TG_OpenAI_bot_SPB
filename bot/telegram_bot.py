
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from openai import OpenAI, BadRequestError
from sqlalchemy import text as sa_text

# === Telegram (PTB 20+) ===
try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        BufferedInputFile,
    )
    HAS_BUFFERED = True
except Exception:  # старые сборки PTB без BufferedInputFile
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup  # type: ignore
    HAS_BUFFERED = False

from telegram import InputFile
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# SETTINGS & DB SESSION
# =========================
# Поддерживаем оба пути импорта настроек
try:
    from bot.settings import load_settings as _load_settings
    settings = _load_settings()
except Exception:
    try:
        from settings import settings  # type: ignore
    except Exception:
        class _S:  # минимальный заглушечный объект
            openai_api_key = os.getenv("OPENAI_API_KEY")
            telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            database_url = os.getenv("DATABASE_URL")
            openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
            kb_top_k = int(os.getenv("KB_TOP_K", "5"))
            yandex_disk_token = os.getenv("YANDEX_DISK_TOKEN")
            admin_user_ids = os.getenv("ADMIN_USER_IDS", "")
        settings = _S()  # type: ignore

# Фабрика сессий (поддерживаем разные размещения файла session.py)
try:
    from bot.session import SessionLocal as _SessionLocal
except Exception:
    try:
        from session import SessionLocal as _SessionLocal  # type: ignore
    except Exception:
        _SessionLocal = None

def session_factory():
    if _SessionLocal is None:
        raise RuntimeError("SessionLocal не найден. Проверьте файл session.py и модульный путь импорта.")
    return _SessionLocal()

# Единственный клиент OpenAI для всего процесса
_OA = OpenAI(api_key=(getattr(settings, "openai_api_key", None) or os.getenv("OPENAI_API_KEY")))

# =========================
# LOGGING
# =========================
log = logging.getLogger(__name__)

# =========================
# SINGLETON (один poller)
# =========================
def _singleton_lock_or_exit():
    """Гарантируем один запущенный экземпляр poller-а через advisory_lock."""
    try:
        with session_factory() as db:
            # Ключ на основе строки подключения и токена бота (от конфликтов разных окружений)
            key_src = f"{getattr(settings, 'database_url', '')}|{getattr(settings, 'telegram_bot_token', '')}"
            lock_key = int(hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:15], 16) % (2**31)
            ok = db.execute(sa_text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar()
            if not ok:
                log.error("❌ Найден другой экземпляр бота (pg_advisory_lock). Завершаю процесс.")
                sys.exit(0)
            log.info("✅ Получен singleton pg_advisory_lock.")
    except Exception:
        log.exception("singleton lock failed (продолжаю без выхода — возможен Conflict от Telegram)")

# =========================
# POST INIT (сброс webhook)
# =========================
async def _post_init(app: "Application"):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("✅ Webhook удалён, pending updates сброшены.")
    except Exception:
        log.exception("drop_webhook failed")

# =========================
# УТИЛИТЫ
# =========================
TELEGRAM_CHUNK = 3500

def _split_for_tg(text: str, limit: int = TELEGRAM_CHUNK) -> List[str]:
    out, s = [], (text or "").strip()
    while len(s) > limit:
        cut = s.rfind("\n\n", 0, limit)
        if cut == -1: cut = s.rfind("\n", 0, limit)
        if cut == -1: cut = s.rfind(" ", 0, limit)
        if cut == -1: cut = limit
        out.append(s[:cut].rstrip())
        s = s[cut:].lstrip()
    if s:
        out.append(s)
    return out

async def _send_long(m, text: str):
    for part in _split_for_tg(text):
        await m.reply_text(part)

def _is_admin(tg_id: int) -> bool:
    try:
        ids = [int(x.strip()) for x in (getattr(settings, "admin_user_ids", "") or "").split(",") if x.strip()]
        return tg_id in ids
    except Exception:
        return False

def _ensure_user(db, tg_id: int) -> int:
    uid = db.execute(sa_text("SELECT id FROM users WHERE tg_user_id=:tg"), {"tg": tg_id}).scalar()
    if uid:
        return int(uid)
    uid = db.execute(sa_text(
        "INSERT INTO users (tg_user_id, is_admin, is_allowed, lang) VALUES (:tg,FALSE,TRUE,'ru') RETURNING id"
    ), {"tg": tg_id}).scalar()
    db.commit()
    return int(uid)

def _create_new_dialog_for_tg(db, tg_id: int) -> int:
    uid = _ensure_user(db, tg_id)
    today = datetime.now().date().isoformat()
    cnt = db.execute(sa_text(
        "SELECT count(*) FROM dialogs WHERE user_id=:u AND is_deleted=FALSE"
    ), {"u": uid}).scalar() or 0
    title = f"{today} | диалог {cnt+1}"
    did = db.execute(sa_text("""
        INSERT INTO dialogs (user_id, title, style, model, is_deleted, created_at)
        VALUES (:u, :t, 'pro', :m, FALSE, now()) RETURNING id
    """), {"u": uid, "t": title, "m": getattr(settings, "openai_model", "gpt-4o-mini")}).scalar()
    db.commit()
    return int(did)

def _get_active_dialog_id(db, tg_id: int) -> int | None:
    row = db.execute(sa_text("""
        SELECT d.id
        FROM dialogs d
        JOIN users u ON u.id = d.user_id
        WHERE u.tg_user_id=:tg AND d.is_deleted=FALSE
        ORDER BY COALESCE(d.last_message_at, to_timestamp(0)) DESC,
                 d.created_at DESC, d.id DESC
        LIMIT 1
    """), {"tg": tg_id}).first()
    return int(row[0]) if row else None

# --- Схема messages может отличаться (text/content). Вставляем безопасно.
_MSG_COLS_CACHE = None
def _detect_messages_layout(db):
    global _MSG_COLS_CACHE
    if _MSG_COLS_CACHE is not None:
        return _MSG_COLS_CACHE
    rows = db.execute(sa_text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'messages'
    """)).all()
    cols = {r[0] for r in rows}
    _MSG_COLS_CACHE = {"text": "text" in cols, "content": "content" in cols}
    return _MSG_COLS_CACHE

def _save_message(db, dialog_id: int, role: str, text: str | None, content: str | None = None):
    cols = _detect_messages_layout(db)
    payload = {"d": dialog_id, "r": role}
    txt = (text or "")[:65535]
    cnt = content if content is not None else txt

    if cols["text"] and cols["content"]:
        payload.update({"t": txt, "c": cnt})
        db.execute(sa_text(
            "INSERT INTO messages (dialog_id, role, text, content) VALUES (:d, :r, :t, :c)"
        ), payload)
    elif cols["text"]:
        payload.update({"t": txt})
        db.execute(sa_text(
            "INSERT INTO messages (dialog_id, role, text) VALUES (:d, :r, :t)"
        ), payload)
    elif cols["content"]:
        payload.update({"c": cnt})
        db.execute(sa_text(
            "INSERT INTO messages (dialog_id, role, content) VALUES (:d, :r, :c)"
        ), payload)
    else:
        payload.update({"t": txt})
        db.execute(sa_text(
            "INSERT INTO messages (dialog_id, role, text) VALUES (:d, :r, :t)"
        ), payload)
    db.commit()

# =========================
# RAG helpers
# =========================
def _get_embedding_model() -> str:
    return getattr(settings, "embedding_model", "text-embedding-3-large")

def _kb_embedding_column_kind(db) -> str:
    try:
        t = db.execute(sa_text("SELECT pg_typeof(embedding)::text FROM kb_chunks LIMIT 1")).scalar()
        if t:
            t = str(t).lower()
            if "vector" in t: return "vector"
            if "bytea"  in t: return "bytea"
        t = db.execute(sa_text("""
            SELECT COALESCE(udt_name, data_type)
            FROM information_schema.columns
            WHERE table_name='kb_chunks' AND column_name='embedding'
            LIMIT 1
        """)).scalar()
        t = (t or "").lower()
        if "vector" in t: return "vector"
        if "bytea"  in t: return "bytea"
    except Exception:
        pass
    return "none"

def _embed_query(text: str) -> List[float]:
    resp = _OA.embeddings.create(model=_get_embedding_model(), input=[text])
    return resp.data[0].embedding

def _vec_literal(vec: List[float]) -> tuple[dict, str]:
    arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
    return {"q": arr}, "CAST(:q AS vector)"

def _retrieve_chunks(db, dialog_id: int, question: str, k: int = 6) -> List[dict]:
    if _kb_embedding_column_kind(db) != "vector":
        return []
    q = _embed_query(question)
    params, qexpr = _vec_literal(q)
    rows = db.execute(sa_text(f"""
        SELECT c.content, c.meta, d.path
        FROM kb_chunks c
        JOIN kb_documents d    ON d.id=c.document_id AND d.is_active
        JOIN dialog_kb_links l ON l.document_id=d.id
        WHERE l.dialog_id=:did
        ORDER BY c.embedding <=> {qexpr}
        LIMIT :k
    """), dict(params, did=dialog_id, k=k)).mappings().all()
    return [dict(r) for r in rows]

def _build_prompt_with_style(ctx_blocks: List[str], user_q: str, dialog_style: str) -> str:
    style_map = {
        "pro":   "Профессионал: максимально ёмко и по делу, шаги и чек-лист.",
        "expert":"Эксперт: подробно, причины/следствия, альтернативы, выводы. Цитаты — в конце.",
        "user":  "Пользователь: простыми словами, примеры и аналогии.",
        "ceo":   "CEO: бизнес-ценность, ROI, риски, сроки, варианты и trade-offs.",
    }
    style_line = style_map.get((dialog_style or "pro").lower(), style_map["pro"])
    header = (
        "Ты — аккуратный ассистент. Используй контекст БЗ, но не ограничивайся цитатами: "
        "синтезируй цельный ответ в выбранном стиле. Если уверенности нет — уточни."
    )
    ctx = "\n\n".join([f"[Фрагмент #{i+1}]\n{t}" for i, t in enumerate(ctx_blocks)])
    return f"{header}\nСтиль: {style_line}\n\nКонтекст:\n{ctx}\n\nВопрос: {user_q}"

def _format_citations(chunks: List[dict]) -> str:
    def short(p: str) -> str:
        return (p or "").split("/")[-1].split("?")[0]
    uniq = []
    for r in chunks:
        name = short(r.get("path") or (r.get("meta") or {}).get("path", ""))
        if name and name not in uniq:
            uniq.append(name)
    return ("\n\nИсточники: " + "; ".join(f"[{i+1}] {n}" for i, n in enumerate(uniq[:5]))) if uniq else ""

async def _chat_full(model: str, messages: list, temperature: float = 0.3, max_turns: int = 6) -> str:
    hist = list(messages)
    full = ""
    turns = 0
    while turns < max_turns:
        turns += 1
        resp = _OA.chat.completions.create(
            model=model,
            messages=hist,
            temperature=temperature,
            max_tokens=1024,
        )
        choice = resp.choices[0]
        piece = choice.message.content or ""
        full += piece
        if choice.finish_reason != "length":
            break
        hist.append({"role": "assistant", "content": piece})
        hist.append({"role": "user", "content": "Продолжай с того места. Не повторяйся."})
    return full

# =========================
# ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)
    m = getattr(update, "effective_message", None)
    if m:
        try:
            await m.reply_text("⚠ Внутренняя ошибка обработчика. Уже чиним.")
        except Exception:
            pass

# =========================
# КОМАНДЫ
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    await m.reply_text(
        "Привет! Я помогу искать ответы в документах из БЗ и вести диалоги в разных стилях.\n"
        "Полный список команд — /help"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    await m.reply_text(
        "/start — приветствие\n"
        "/help — список команд\n"
        "/dialogs — список диалогов (открыть/переименовать/экспорт/удалить)\n"
        "/dialog <id> — сделать диалог активным\n"
        "/dialog_new — создать новый диалог\n"
        "/kb — меню БЗ (подключение/отключение доков)\n"
        "/kb_sync — синхронизация БЗ (админ)\n"
        "/kb_diag — диагностика БЗ\n"
        "/stats — карточка активного диалога\n"
        "/web <запрос> — веб-поиск (если включён)\n"
        "/repair_schema — починка схемы БД (админ)\n"
        "/dbcheck — проверка наличия таблиц\n"
        "/migrate — применить Alembic миграции (админ)\n"
        "/pgvector_check — наличие/установка pgvector\n"
        "/whoami — мои права\n"
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg = update.effective_user.id
        with session_factory() as db:
            row = db.execute(sa_text(
                "SELECT is_admin, is_allowed, lang FROM users WHERE tg_user_id=:tg"
            ), {"tg": tg}).first()
        is_admin = bool(row[0]) if row else False
        is_allowed = bool(row[1]) if row else True
        lang = (row[2] or "ru") if row else "ru"
        await (update.message or update.effective_message).reply_text(
            f"whoami: tg={tg}, role={'admin' if is_admin else ('allowed' if is_allowed else 'guest')}, lang={lang}"
        )
    except Exception:
        log.exception("whoami failed")
        await (update.message or update.effective_message).reply_text("⚠ Ошибка whoami")

# ---- Диалоги ----
async def dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with session_factory() as db:
            uid = _ensure_user(db, update.effective_user.id)
            rows = db.execute(sa_text("""
                SELECT d.id, COALESCE(NULLIF(d.title,''), CONCAT('Диалог ', d.id)) AS title
                FROM dialogs d
                WHERE d.user_id = :u AND d.is_deleted = FALSE
                ORDER BY COALESCE(d.last_message_at, d.created_at) DESC, d.id DESC
                LIMIT 50
            """), {"u": uid}).all()

        kb_rows = []
        for did, title in rows:
            kb_rows.append([
                InlineKeyboardButton(title, callback_data=f"dlg:open:{did}"),
                InlineKeyboardButton("✏️",  callback_data=f"dlg:rename:{did}"),
                InlineKeyboardButton("📤",  callback_data=f"dlg:export:{did}"),
                InlineKeyboardButton("🗑️",  callback_data=f"dlg:delete:{did}"),
            ])
        kb_rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")])

        await m.reply_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(kb_rows))
    except Exception:
        log.exception("dialogs failed")
        await m.reply_text("⚠ Ошибка /dialogs")

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data == "dlg:new":
            with session_factory() as db:
                did = _create_new_dialog_for_tg(db, update.effective_user.id)
            return await q.edit_message_text(f"✅ Создан диалог #{did}")
        if data.startswith("dlg:open:"):
            did = int(data.split(":")[-1])
            context.user_data["active_dialog_id"] = did
            return await q.edit_message_text(f"Открыт диалог #{did}")
        if data.startswith("dlg:rename:"):
            did = int(data.split(":")[-1])
            context.user_data["rename_dialog_id"] = did
            return await q.edit_message_text("Введите новое название диалога:")
        if data.startswith("dlg:export:"):
            did = int(data.split(":")[-1])
            with session_factory() as db:
                msgs = db.execute(sa_text("""
                    SELECT role, COALESCE(content, text) AS body, created_at
                    FROM messages WHERE dialog_id=:d ORDER BY created_at
                """), {"d": did}).all()
            lines = ["# Экспорт диалога", ""]
            for role, body, _ in msgs:
                who = "Пользователь" if role == "user" else "Бот"
                lines.append(f"**{who}:**\n{body or ''}\n")
            data_bytes = "\n".join(lines).encode("utf-8")
            if HAS_BUFFERED:
                await q.message.reply_document(document=BufferedInputFile(data_bytes, filename=f"dialog_{did}.md"),
                                               caption="Экспорт готов")
            else:
                await q.message.reply_document(document=InputFile(BytesIO(data_bytes), filename=f"dialog_{did}.md"),
                                               caption="Экспорт готов")
            return
        if data.startswith("dlg:delete:"):
            did = int(data.split(":")[-1])
            with session_factory() as db:
                db.execute(sa_text("UPDATE dialogs SET is_deleted=TRUE WHERE id=:d"), {"d": did})
                db.commit()
            return await q.edit_message_text(f"Диалог #{did} удалён")
    except Exception:
        log.exception("dialog_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка обработчика /dialogs.")
        except Exception:
            pass

async def dialog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    args = context.args or []
    if not args:
        return await dialogs(update, context)
    try:
        did = int(args[0])
    except Exception:
        return await m.reply_text("Использование: /dialog <id>")
    context.user_data["active_dialog_id"] = did
    await m.reply_text(f"✅ Активный диалог: {did}")
    return await stats(update, context)

async def dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with session_factory() as db:
            did = _create_new_dialog_for_tg(db, update.effective_user.id)
        await m.reply_text(f"✅ Создан диалог #{did}")
    except Exception:
        log.exception("dialog_new failed")
        await m.reply_text("⚠ Ошибка создания диалога")

# ---- KB ----
async def kb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    db = session_factory()
    try:
        doc_cnt   = db.execute(sa_text("SELECT count(*) FROM kb_documents WHERE is_active = true")).scalar_one()
        chunk_cnt = db.execute(sa_text("SELECT count(*) FROM kb_chunks")).scalar_one()
        link_cnt  = db.execute(sa_text("SELECT count(*) FROM dialog_kb_links")).scalar_one()

        await m.reply_text(
            f"БЗ: документов активных — {doc_cnt}, чанков — {chunk_cnt}, привязок к диалогам — {link_cnt}"
        )
        keyboard = [
            [InlineKeyboardButton("🗘 Синхронизация", callback_data="kb:sync")],
            [InlineKeyboardButton("📊 Диагностика",   callback_data="kb:diag")],
        ]
        await m.reply_text("Меню БЗ:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        log.exception("kb_cmd failed")
        await m.reply_text("⚠ Ошибка /kb")
    finally:
        db.close()

async def kb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "kb:sync":
        await q.edit_message_text("🔄 Стартую синхронизацию БЗ…")
        return await kb_sync(update, context)
    if data == "kb:diag":
        await kb_diag(update, context)
        try:
            await q.delete_message()
        except Exception:
            pass
        return

async def kb_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")
    await m.reply_text("🔄 Синхронизация запущена...")
    try:
        import inspect
        # ожидаем модуль bot.knowledge_base.indexer или knowledge_base/indexer.py
        try:
            from bot.knowledge_base import indexer  # type: ignore
        except Exception:
            import importlib
            indexer = importlib.import_module("indexer")  # fallback на корень

        entry = getattr(settings, "kb_sync_entrypoint", None) or os.getenv("KB_SYNC_ENTRYPOINT", None)
        fn = getattr(indexer, entry, None) if entry else None
        if not fn:
            for cand in ("sync_kb","sync_all","sync_from_yandex","sync","run_sync","full_sync","reindex","index_all","ingest_all","ingest","main"):
                if hasattr(indexer, cand) and callable(getattr(indexer, cand)):
                    fn = getattr(indexer, cand); break
        if not fn:
            raise RuntimeError("Не найден entrypoint в indexer.py. Укажите KB_SYNC_ENTRYPOINT или реализуйте sync_kb(session).")

        sig = inspect.signature(fn)
        kwargs, to_close = {}, None
        for p in sig.parameters.values():
            nm = p.name.lower()
            if nm in ("session","db","dbsession","conn","connection"):
                sess = session_factory(); kwargs[p.name] = sess; to_close = sess
            elif nm in ("sessionlocal","session_factory","factory","engine"):
                kwargs[p.name] = session_factory
            elif nm in ("settings","cfg","config","conf"):
                kwargs[p.name] = settings

        def _call():
            try:
                return fn(**kwargs)
            finally:
                if to_close is not None:
                    try: to_close.close()
                    except Exception: pass

        res = await asyncio.to_thread(_call)
        if isinstance(res, dict):
            upd = res.get("updated"); skp = res.get("skipped"); tot = res.get("total")
            msg = "✅ Синхронизация завершена."
            if any(v is not None for v in (upd, skp, tot)):
                msg += f" Обновлено: {upd or 0}, пропущено: {skp or 0}, всего: {tot or 0}."
            return await m.reply_text(msg)
        elif isinstance(res, (tuple, list)) and len(res) >= 2:
            return await m.reply_text(f"✅ Готово: документов {res[0]}, чанков {res[1]}")
        else:
            return await m.reply_text("✅ Синхронизация завершена.")
    except Exception as e:
        log.exception("kb_sync failed")
        return await m.reply_text(f"⚠ Ошибка синхронизации: {e}")

async def kb_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with session_factory() as db:
            docs = db.execute(sa_text("SELECT count(*) FROM kb_documents WHERE is_active")).scalar() or 0
            chunks = db.execute(sa_text("SELECT count(*) FROM kb_chunks")).scalar() or 0
            links = db.execute(sa_text("SELECT count(*) FROM dialog_kb_links")).scalar() or 0
        await m.reply_text(f"БЗ: документов активных — {docs}, чанков — {chunks}, привязок к диалогам — {links}")
    except Exception:
        log.exception("kb_diag failed")
        await m.reply_text("⚠ Ошибка kb_diag")

# ---- WEB ----
async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply_text("Использование: /web <запрос>")
    query = parts[1].strip()
    try:
        from bot.web_search import web_search_digest, sources_footer  # type: ignore
        answer, sources = await web_search_digest(query, max_results=6, openai_api_key=getattr(settings, "openai_api_key", None))
        footer = ("\n\nИсточники:\n" + sources_footer(sources)) if sources else ""
        await _send_long(m, (answer or "Готово.") + footer)
        if sources:
            buttons = [[InlineKeyboardButton(f"[{i+1}] {urlparse(s['url']).netloc}", url=s['url'])] for i, s in enumerate(sources)]
            await m.reply_text("Открыть источники:", reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
    except Exception as e:
        await m.reply_text(
            "🔎 Веб-поиск пока отключён или недоступен (нет ключей Tavily/SerpAPI/Bing).\n"
            "Я могу ответить своими знаниями или найти в БЗ через /kb.\n"
            f"Детали: {e}"
        )

# ---- STATS ----
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with session_factory() as db:
            did = context.user_data.get("active_dialog_id") or _get_active_dialog_id(db, update.effective_user.id)
            if not did:
                return await m.reply_text("Нет активного диалога. Создайте через /dialog_new или выберите /dialogs.")

            row = db.execute(sa_text(
                "SELECT id, title, model, style, created_at, last_message_at FROM dialogs WHERE id=:d"
            ), {"d": did}).first()
            msgs = db.execute(sa_text("SELECT count(*) FROM messages WHERE dialog_id=:d"), {"d": did}).scalar() or 0
            docs = db.execute(sa_text("""
                SELECT d.path
                FROM kb_documents d
                JOIN dialog_kb_links l ON l.document_id=d.id
                WHERE l.dialog_id=:d ORDER BY d.path
            """), {"d": did}).fetchall()
            total_dialogs = db.execute(sa_text("""
                SELECT count(*) FROM dialogs WHERE user_id=(
                    SELECT id FROM users WHERE tg_user_id=:tg LIMIT 1
                ) AND is_deleted=FALSE
            """), {"tg": update.effective_user.id}).scalar() or 0

        title = row[1] if row else "-"
        model = row[2] if row else getattr(settings, "openai_model", "gpt-4o-mini")
        style = row[3] if row else "pro"
        created = row[4] if row else "-"
        changed = row[5] if row else "-"
        doc_list = "\n".join(f"• {r[0]}" for r in docs) or "—"

        text = (
            f"Диалог: {did} — {title}\n"
            f"Модель: {model} | Стиль: {style}\n"
            f"Создан: {created or '-'} | Изменён: {changed or '-'}\n"
            f"Подключённые документы ({len(docs)}):\n{doc_list}\n\n"
            f"Всего твоих диалогов: {int(total_dialogs)} | Сообщений в этом диалоге: {int(msgs)}"
        )
        return await m.reply_text(text)
    except Exception:
        log.exception("/stats failed")
        return await m.reply_text("⚠ Ошибка /stats")

# ---- TEXT / VOICE ----
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    text = (m.text or "").strip()
    if not text:
        return
    try:
        with session_factory() as db:
            tg = update.effective_user.id
            _ensure_user(db, tg)
            did = context.user_data.get("active_dialog_id") or _get_active_dialog_id(db, tg) or _create_new_dialog_for_tg(db, tg)

            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            model = (row[0] if row and row[0] else getattr(settings, "openai_model", "gpt-4o-mini"))
            style = (row[1] if row and row[1] else "pro")

            _save_message(db, did, "user", text)

            top_k = int(getattr(settings, "kb_top_k", 5))
            rows = _retrieve_chunks(db, did, text, k=top_k)

        ctx_blocks = [r["content"] for r in rows]
        prompt = _build_prompt_with_style(ctx_blocks, text, style)
        messages = [{"role":"system","content":"RAG assistant"}, {"role":"user","content":prompt}]

        answer = await _chat_full(model, messages, temperature=0.3, max_turns=6)
        cites = _format_citations(rows)
        final = answer + (cites if cites else "")

        await _send_long(m, final)

        with session_factory() as db:
            _save_message(db, did, "assistant", final)

    except Exception:
        log.exception("on_text failed")
        await m.reply_text("⚠ Не удалось обработать сообщение. Попробуйте ещё раз.")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    v = m.voice or m.audio
    if not v:
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")

    tmp_path = None
    try:
        # 1) Скачиваем .ogg
        file = await v.get_file()
        fd, p = tempfile.mkstemp(suffix=".ogg"); os.close(fd)
        tmp_path = Path(p)
        await file.download_to_drive(str(tmp_path))

        # 2) Транскрибуем
        with open(tmp_path, "rb") as fh:
            tr = _OA.audio.transcriptions.create(
                model=getattr(settings, "ASR_MODEL", "gpt-4o-transcribe"),
                file=fh,
                # language="ru",
            )
        text = (getattr(tr, "text", "") or "").strip()
        if not text:
            return await m.reply_text("⚠ Не удалось распознать речь. Скажите ещё раз, пожалуйста.")

        # 3) Тот же пайплайн, что и on_text
        with session_factory() as db:
            tg = update.effective_user.id
            _ensure_user(db, tg)
            did = context.user_data.get("active_dialog_id") or _get_active_dialog_id(db, tg) or _create_new_dialog_for_tg(db, tg)

            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            model = (row[0] if row and row[0] else getattr(settings, "openai_model", "gpt-4o-mini"))
            style = (row[1] if row and row[1] else "pro")

            _save_message(db, did, "user", text)

            top_k = int(getattr(settings, "kb_top_k", 5))
            rows = _retrieve_chunks(db, did, text, k=top_k)

        ctx_blocks = [r["content"] for r in rows]
        prompt = _build_prompt_with_style(ctx_blocks, text, style)
        messages = [{"role":"system","content":"RAG assistant"}, {"role":"user","content":prompt}]

        answer = await _chat_full(model, messages, temperature=0.3, max_turns=6)
        cites = _format_citations(rows)
        final = answer + (cites if cites else "")

        await _send_long(m, final)

        with session_factory() as db:
            _save_message(db, did, "assistant", final)

    except BadRequestError as e:
        log.error("ASR BadRequest: %s", e, exc_info=True)
        return await m.reply_text("⚠ Не удалось обработать голосовое (формат/качество). Попробуйте ещё раз.")
    except Exception:
        log.exception("on_voice failed")
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")
    finally:
        try:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

# ---- SERVICE ----
async def dbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with session_factory() as db:
            rows = db.execute(sa_text("""
                select 'users' as t, to_regclass('public.users') is not null
                union all select 'dialogs',          to_regclass('public.dialogs') is not null
                union all select 'messages',         to_regclass('public.messages') is not null
                union all select 'kb_documents',     to_regclass('public.kb_documents') is not null
                union all select 'kb_chunks',        to_regclass('public.kb_chunks') is not null
                union all select 'dialog_kb_links',  to_regclass('public.dialog_kb_links') is not null
                union all select 'pdf_passwords',    to_regclass('public.pdf_passwords') is not null
                union all select 'audit_log',        to_regclass('public.audit_log') is not null
            """)).all()
        lines = ["Проверка таблиц:"]
        for t, ok in rows:
            lines.append(f"{'✅' if ok else '❌'} {t}")
        await (update.effective_message or update.message).reply_text("\n".join(lines))
    except Exception:
        log.exception("dbcheck failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка dbcheck")

async def migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.effective_message or update.message).reply_text("Только для админа.")
        await (update.effective_message or update.message).reply_text("🔧 Запускаю миграции...")
        from alembic.config import Config
        from alembic import command
        os.environ["DATABASE_URL"] = getattr(settings, "database_url", os.getenv("DATABASE_URL", ""))
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        await (update.effective_message or update.message).reply_text("✅ Миграции применены.")
    except Exception:
        log.exception("migrate failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка миграции.")

async def repair_schema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Только админам.")
    await m.reply_text("🧱 Ремонт схемы начат...")
    try:
        with session_factory() as db:
            # users (с tg_user_id как в существующей схеме)
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS users(
                id BIGSERIAL PRIMARY KEY,
                tg_user_id BIGINT UNIQUE NOT NULL,
                is_admin BOOLEAN DEFAULT FALSE,
                is_allowed BOOLEAN DEFAULT TRUE,
                lang TEXT DEFAULT 'ru',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
            )"""))

            # dialogs
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS dialogs(
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT,
                model TEXT,
                style TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                last_message_at TIMESTAMP WITH TIME ZONE,
                is_deleted BOOLEAN DEFAULT FALSE
            )"""))

            # messages (поддержка и content, и text)
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS messages(
                id BIGSERIAL PRIMARY KEY,
                dialog_id BIGINT NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT,
                text TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
            )"""))

            # kb_documents (updated_at с DEFAULT now(), чтобы избежать NOT NULL ошибок)
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS kb_documents(
                id BIGSERIAL PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                etag TEXT,
                mime TEXT,
                pages INT,
                bytes BIGINT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                is_active BOOLEAN DEFAULT TRUE
            )"""))

            # kb_chunks (если pgvector есть — делаем vector, иначе массив)
            db.execute(sa_text("""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector') THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.tables WHERE table_name='kb_chunks'
                ) THEN
                  CREATE TABLE kb_chunks(
                    id BIGSERIAL PRIMARY KEY,
                    document_id BIGINT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                    chunk_index INT NOT NULL,
                    content TEXT NOT NULL,
                    embedding DOUBLE PRECISION[]
                  );
                  CREATE INDEX idx_chunks_doc_ix ON kb_chunks(document_id, chunk_index);
                END IF;
              ELSE
                CREATE TABLE IF NOT EXISTS kb_chunks(
                    id BIGSERIAL PRIMARY KEY,
                    document_id BIGINT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                    chunk_index INT NOT NULL,
                    content TEXT NOT NULL,
                    embedding VECTOR(3072)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc_ix ON kb_chunks(document_id, chunk_index);
                CREATE INDEX IF NOT EXISTS kb_chunks_vec_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists=100);
              END IF;
            END$$;"""))

            # связка диалогов и доков
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS dialog_kb_links(
                dialog_id BIGINT NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
                document_id BIGINT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                PRIMARY KEY(dialog_id, document_id)
            )"""))

            # pdf пароли
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS pdf_passwords(
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                doc_path TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                PRIMARY KEY(user_id, doc_path)
            )"""))

            # аудит
            db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS audit_log(
                id BIGSERIAL PRIMARY KEY,
                at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                user_id BIGINT,
                action TEXT,
                payload JSONB
            )"""))

            db.commit()

        await m.reply_text("✅ Ремонт завершён.")
    except Exception:
        log.exception("repair_schema failed")
        await m.reply_text("⚠ Ошибка /repair_schema")

async def pgvector_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with session_factory() as db:
            avail = db.execute(sa_text("SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')")).scalar()
            installed = db.execute(sa_text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")).scalar()
        await (update.effective_message or update.message).reply_text(
            f"pgvector доступно: {'✅' if avail else '❌'}\n"
            f"pgvector установлено: {'✅' if installed else '❌'}"
        )
    except Exception:
        log.exception("pgvector_check failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка pgvector_check")

# =========================
# BUILD APP
# =========================
def build_app() -> Application:
    _singleton_lock_or_exit()
    app = ApplicationBuilder().token(getattr(settings, "telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN"))).post_init(_post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))

    app.add_handler(CommandHandler("dialogs", dialogs))
    app.add_handler(CallbackQueryHandler(dialog_cb, pattern=r"^dlg:"))

    app.add_handler(CommandHandler("dialog", dialog_cmd))
    app.add_handler(CommandHandler("dialog_new", dialog_new))

    app.add_handler(CommandHandler("kb", kb_cmd))
    app.add_handler(CallbackQueryHandler(kb_cb, pattern=r"^kb:"))
    app.add_handler(CommandHandler("kb_sync", kb_sync))
    app.add_handler(CommandHandler("kb_diag", kb_diag))

    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("web", web_cmd))

    app.add_handler(CommandHandler("repair_schema", repair_schema))
    app.add_handler(CommandHandler("dbcheck", dbcheck))
    app.add_handler(CommandHandler("migrate", migrate))
    app.add_handler(CommandHandler("pgvector_check", pgvector_check))

    # Сообщения
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)

    return app
