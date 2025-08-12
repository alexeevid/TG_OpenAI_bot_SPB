# bot/telegram_bot.py
from __future__ import annotations

# == БАЗОВЫЕ ИМПОРТЫ ==
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from typing import List, Tuple
from urllib.parse import urlparse

import tiktoken
from openai import OpenAI
from sqlalchemy import text as sa_text

# PTB 20.x
try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        BufferedInputFile,
        InputFile,
    )
    HAS_BUFFERED = True
except Exception:  # старые сборки PTB
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile  # type: ignore
    HAS_BUFFERED = False

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# == НАСТРОЙКИ / БД ==
from bot.settings import load_settings
from bot.db.session import SessionLocal  # engine подтягивается через Alembic при необходимости

log = logging.getLogger(__name__)
settings = load_settings()

# OpenAI клиент
_OA = OpenAI(api_key=(getattr(settings, "openai_api_key", None) or getattr(settings, "OPENAI_API_KEY", None)))

# ---------- SINGLETON LOCK (исключаем два параллельных poller-а) ----------
import psycopg2

_singleton_conn = None  # держим подключение живым (держит advisory_lock)

def _ensure_single_instance() -> None:
    """Берём pg_advisory_lock на процесс. Если занят — выходим, чтобы не ловить Conflict от Telegram."""
    global _singleton_conn
    if _singleton_conn is not None:
        return
    dsn = getattr(settings, "database_url", None) or getattr(settings, "DATABASE_URL", None)
    if not dsn:
        log.warning("DATABASE_URL не задан — singleton-lock пропущен (риск Conflict).")
        return
    try:
        key_src = f"{dsn}|{getattr(settings,'telegram_bot_token',None) or getattr(settings,'TELEGRAM_BOT_TOKEN',None)}"
        lock_key = int(hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:15], 16) % (2**31)
        _singleton_conn = psycopg2.connect(dsn)
        _singleton_conn.autocommit = True
        with _singleton_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            ok = cur.fetchone()[0]
        if not ok:
            log.error("‼️ Уже запущен другой экземпляр бота (advisory-lock занят). Завершаюсь.")
            sys.exit(0)
        log.info("✅ Получен singleton pg_advisory_lock.")
    except Exception:
        log.exception("Не удалось взять singleton-lock — продолжаю без него (риск Conflict).")

# ---------- post_init: очищаем webhook перед polling ----------
async def _post_init(app: "Application"):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("✅ Webhook удалён и обновления очищены.")
    except Exception:
        log.exception("Не удалось удалить webhook")

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
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
        raw = getattr(settings, "admin_user_ids", None) or getattr(settings, "ADMIN_USER_IDS", "")
        ids = [int(x.strip()) for x in str(raw).split(",") if x.strip()]
        return tg_id in ids
    except Exception:
        return False

def _ensure_user(db, tg_id: int) -> int:
    uid = db.execute(sa_text("SELECT id FROM users WHERE tg_user_id=:tg"), {"tg": tg_id}).scalar()
    if uid:
        return int(uid)
    uid = db.execute(
        sa_text("INSERT INTO users (tg_user_id, is_admin, is_allowed, lang) VALUES (:tg,FALSE,TRUE,'ru') RETURNING id"),
        {"tg": tg_id},
    ).scalar()
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
    """), {"u": uid, "t": title, "m": (getattr(settings,"openai_model",None) or getattr(settings,"OPENAI_MODEL","gpt-4o-mini"))}).scalar()
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

def _save_message(db, dialog_id: int, role: str, content: str):
    enc = tiktoken.get_encoding("cl100k_base")
    toks = len(enc.encode(content or ""))
    db.execute(sa_text("""
        INSERT INTO messages (dialog_id, role, content, tokens)
        VALUES (:d,:r,:c,:t)
    """), {"d": dialog_id, "r": role, "c": content, "t": toks})
    # Обновим last_message_at
    db.execute(sa_text("UPDATE dialogs SET last_message_at=now() WHERE id=:d"), {"d": dialog_id})
    db.commit()

# ---------- OpenAI / RAG ----------
def _get_embedding_model() -> str:
    return getattr(settings, "embedding_model", None) or getattr(settings, "OPENAI_EMBEDDING_MODEL", None) or "text-embedding-3-large"

def _embed_query(text: str) -> List[float]:
    resp = _OA.embeddings.create(model=_get_embedding_model(), input=[text])
    return resp.data[0].embedding

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

def _vec_literal(vec: List[float]) -> tuple[dict, str]:
    arr = "[" + ","join(f"{x:.6f}" for x in (vec or [])) + "]"  # noqa
    # ОШИБКА ↑: исправим ниже правильной реализацией
    return {"q": arr}, "CAST(:q AS vector)"

# Исправленная версия (оставляем обе, но используем правильную)
def _vec_literal_fixed(vec: List[float]) -> tuple[dict, str]:
    arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
    return {"q": arr}, "CAST(:q AS vector)"

def _retrieve_chunks(db, dialog_id: int, question: str, k: int = 6) -> List[dict]:
    if _kb_embedding_column_kind(db) != "vector":
        return []
    q = _embed_query(question)
    params, qexpr = _vec_literal_fixed(q)
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

_STYLE_EXAMPLES = {
    "pro":    "Кратко, по шагам, чек-лист. Без воды.",
    "expert": "Глубоко и обстоятельно: причины/следствия, альтернативы, ссылки.",
    "user":   "Просто, с примерами из жизни, без жаргона.",
    "ceo":    "Фокус на ценности, рисках, сроках, ROI и решениях.",
}

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

# ---------- КОМАНДЫ ----------
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
        with SessionLocal() as db:
            row = db.execute(sa_text("SELECT is_admin, is_allowed, lang FROM users WHERE tg_user_id=:tg"), {"tg": tg}).first()
        is_admin = bool(row[0]) if row else False
        is_allowed = bool(row[1]) if row else True
        lang = (row[2] or "ru") if row else "ru"
        await (update.message or update.effective_message).reply_text(
            f"whoami: tg={tg}, role={'admin' if is_admin else ('allowed' if is_allowed else 'guest')}, lang={lang}"
        )
    except Exception:
        log.exception("whoami failed")
        await (update.message or update.effective_message).reply_text("⚠ Ошибка whoami")

# ---- Диалоги: список/новый/переключение/экспорт/переименование ----
KB_PAGE_SIZE = 10

async def dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    page = int((context.user_data.get("dlg_page") or 1))
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            total = db.execute(sa_text("SELECT count(*) FROM dialogs WHERE user_id=:u AND is_deleted=FALSE"), {"u": uid}).scalar() or 0
            if total == 0:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")]])
                return await m.reply_text("Диалогов нет.", reply_markup=kb)

            pages = max(1, (total + KB_PAGE_SIZE - 1) // KB_PAGE_SIZE)
            page = max(1, min(page, pages))
            offset = (page - 1) * KB_PAGE_SIZE

            rows = db.execute(sa_text("""
                SELECT id, title, model, style, created_at, last_message_at
                FROM dialogs
                WHERE user_id=:u AND is_deleted=FALSE
                ORDER BY COALESCE(last_message_at, created_at) DESC
                LIMIT :lim OFFSET :off
            """), {"u": uid, "lim": KB_PAGE_SIZE, "off": offset}).all()

        buttons = []
        for (did, title, model, style, created_at, last_msg) in rows:
            label = f"{did} | {title or '(без названия)'}"
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"dlg:open:{did}"),
                InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{did}"),
                InlineKeyboardButton("📤", callback_data=f"dlg:export:{did}"),
                InlineKeyboardButton("🗑️", callback_data=f"dlg:delete:{did}"),
            ])

        nav = []
        if pages > 1:
            if page > 1:
                nav.append(InlineKeyboardButton("« Назад", callback_data=f"dlg:page:{page-1}"))
            nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="dlg:nop"))
            if page < pages:
                nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"dlg:page:{page+1}"))

        foot = [InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")]
        kb = InlineKeyboardMarkup(buttons + ([nav] if nav else []) + [[b] for b in [foot]])

        await m.reply_text("Мои диалоги:", reply_markup=kb)
    except Exception:
        log.exception("dialogs failed")
        await m.reply_text("⚠ Ошибка /dialogs")

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data == "dlg:nop":
            return
        if data.startswith("dlg:page:"):
            page = int(data.split(":")[-1])
            context.user_data["dlg_page"] = page
            await q.message.delete()
            return await dialogs(update, context)
        if data == "dlg:new":
            with SessionLocal() as db:
                did = _create_new_dialog_for_tg(db, update.effective_user.id)
            return await q.edit_message_text(f"✅ Создан диалог #{did}")
        if data.startswith("dlg:open:"):
            did = int(data.split(":")[-1])
            # активируем диалог в user_data
            context.user_data["active_dialog_id"] = did
            return await q.edit_message_text(f"Открыт диалог #{did}")
        if data.startswith("dlg:rename:"):
            did = int(data.split(":")[-1])
            context.user_data["rename_dialog_id"] = did
            return await q.edit_message_text("Введите новое название диалога:")
        if data.startswith("dlg:export:"):
            did = int(data.split(":")[-1])
            with SessionLocal() as db:
                msgs = db.execute(sa_text("""
                    SELECT role, content, created_at
                    FROM messages
                    WHERE dialog_id=:d ORDER BY created_at
                """), {"d": did}).all()
            lines = ["# Экспорт диалога", ""]
            for role, content, _ in msgs:
                who = "Пользователь" if role == "user" else "Бот"
                lines.append(f"**{who}:**\n{content}\n")
            data_bytes = "\n".join(lines).encode("utf-8")
            file = BufferedInputFile(data_bytes, filename=f"dialog_{did}.md") if HAS_BUFFERED else InputFile(data_bytes, filename=f"dialog_{did}.md")  # type: ignore
            await q.message.reply_document(document=file, caption="Экспорт готов")
            return
        if data.startswith("dlg:delete:"):
            did = int(data.split(":")[-1])
            with SessionLocal() as db:
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
    # просто сохраняем в user_data
    context.user_data["active_dialog_id"] = did
    await m.reply_text(f"✅ Активный диалог: {did}")
    return await stats(update, context)

async def dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            did = _create_new_dialog_for_tg(db, update.effective_user.id)
        await m.reply_text(f"✅ Создан диалог #{did}")
    except Exception:
        log.exception("dialog_new failed")
        await m.reply_text("⚠ Ошибка создания диалога")

# ---- KB / SYNC / DIAG ----
def ya_download(path: str) -> bytes:
    import requests
    YA_API = "https://cloud-api.yandex.net/v1/disk"
    headers = {"Authorization": f"OAuth {getattr(settings,'yandex_disk_token',None) or getattr(settings,'YANDEX_DISK_TOKEN','')}"}
    r = requests.get(f"{YA_API}/resources/download", headers=headers, params={"path": path}, timeout=60)
    r.raise_for_status()
    href = (r.json() or {}).get("href")
    if not href:
        raise RuntimeError("download href not returned by Yandex Disk")
    f = requests.get(href, timeout=300)
    f.raise_for_status()
    return f.content

def _ya_list_files(root_path: str):
    import requests
    YA_API = "https://cloud-api.yandex.net/v1/disk"
    headers = {"Authorization": f"OAuth {getattr(settings,'yandex_disk_token',None) or getattr(settings,'YANDEX_DISK_TOKEN','')}"}
    out = []
    limit, offset = 200, 0
    while True:
        r = requests.get(
            f"{YA_API}/resources",
            headers=headers,
            params={
                "path": root_path,
                "limit": limit,
                "offset": offset,
                "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.mime_type,_embedded.items.size,_embedded.items.md5",
            },
            timeout=30,
        )
        r.raise_for_status()
        items = (r.json().get("_embedded") or {}).get("items") or []
        for it in items:
            if it.get("type") == "file":
                out.append(it)
        if len(items) < limit:
            break
        offset += limit
    return out

def _pdf_extract_text(pdf_bytes: bytes) -> tuple[str, int, bool]:
    import fitz  # PyMuPDF
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        is_prot = bool(doc.is_encrypted)
        if is_prot:
            try:
                if doc.authenticate(""):
                    is_prot = False
            except Exception:
                pass
        if is_prot:
            return ("", 0, True)
        pages = doc.page_count
        out = []
        for i in range(pages):
            try:
                out.append(doc.load_page(i).get_text("text") or "")
            except Exception:
                out.append("")
        txt = "\n".join(out)
    # fallback pdfminer если пусто
    if not txt.strip():
        try:
            from pdfminer.high_level import extract_text
            txt = extract_text(BytesIO(pdf_bytes)) or ""
        except Exception:
            pass
    return (txt, pages, False)

def _chunk_text(text: str, max_tokens: int = 2000, overlap: int = 0) -> List[str]:
    enc = tiktoken.get_encoding("cl100k_base")
    toks = enc.encode(text or "")
    out = []
    i = 0
    while i < len(toks):
        part = enc.decode(toks[i:i+max_tokens]).strip()
        if part:
            out.append(part)
        i = i + max_tokens if overlap <= 0 else i + max_tokens - overlap
    return out

async def kb_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")
    await m.reply_text("🔄 Синхронизация запущена...")
    try:
        # Пробуем найти entrypoint в bot/knowledge_base/indexer.py
        import inspect
        from bot.knowledge_base import indexer
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
                sess = SessionLocal(); kwargs[p.name] = sess; to_close = sess
            elif nm in ("sessionlocal","session_factory","factory","engine"):
                kwargs[p.name] = SessionLocal
            elif nm in ("settings","cfg","config","conf"):
                kwargs[p.name] = settings

        def _call():
            try: return fn(**kwargs)
            finally:
                if to_close is not None:
                    try: to_close.close()
                    except Exception: pass

        res = await asyncio.to_thread(_call)
        return await m.reply_text("✅ Синхронизация завершена." if res is None else f"✅ Готово: {res}")
    except Exception as e:
        log.exception("kb_sync failed")
        return await m.reply_text(f"⚠ Ошибка синхронизации: {e}")

async def kb_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            docs = db.execute(sa_text("SELECT count(*) FROM kb_documents WHERE is_active")).scalar() or 0
            chunks = db.execute(sa_text("SELECT count(*) FROM kb_chunks")).scalar() or 0
            links = db.execute(sa_text("SELECT count(*) FROM dialog_kb_links")).scalar() or 0
        await m.reply_text(f"БЗ: документов активных — {docs}, чанков — {chunks}, привязок к диалогам — {links}")
    except Exception:
        log.exception("kb_diag failed")
        await m.reply_text("⚠ Ошибка kb_diag")

async def kb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простейшее меню БЗ. (Поддержка расширенного UI может быть в отдельном модуле.)"""
    m = update.effective_message or update.message
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Синхронизация", callback_data="kb:sync")],
        [InlineKeyboardButton("📊 Диагностика", callback_data="kb:diag")],
    ])
    await m.reply_text("Меню БЗ:", reply_markup=kb)

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

# ---- WEB ----
async def web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply_text("Использование: /web <запрос>")
    query = parts[1].strip()
    await m.reply_text(
        "🔎 Веб-поиск пока отключён в этой сборке (ключи внешнего поиска не заданы).\n"
        "Я могу ответить своими знаниями или попробовать найти в БЗ через /kb."
    )

# ---- СТАТИСТИКА ----
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
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
        model = row[2] if row else (getattr(settings,"openai_model",None) or "gpt-4o-mini")
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

# ---- ТЕКСТ / ГОЛОС ----
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    text = (m.text or "").strip()
    if not text:
        return
    try:
        with SessionLocal() as db:
            tg = update.effective_user.id
            uid = _ensure_user(db, tg)
            did = context.user_data.get("active_dialog_id") or _get_active_dialog_id(db, tg) or _create_new_dialog_for_tg(db, tg)

            # узнаём модель/стиль из диалога
            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            model = (row[0] if row and row[0] else (getattr(settings,"openai_model",None) or "gpt-4o-mini"))
            style = (row[1] if row and row[1] else "pro")

            # сохраняем сообщение пользователя
            _save_message(db, did, "user", text)

            # RAG: достаём чанки
            top_k = int(getattr(settings,"kb_top_k",None) or getattr(settings,"KB_TOP_K",5))
            rows = _retrieve_chunks(db, did, text, k=top_k)

        ctx_blocks = [r["content"] for r in rows]
        prompt = _build_prompt_with_style(ctx_blocks, text, style)
        messages = [{"role":"system","content":"RAG assistant"}, {"role":"user","content":prompt}]

        answer = await _chat_full(model, messages, temperature=0.3, max_turns=6)
        cites = _format_citations(rows)
        final = answer + (cites if cites else "")

        await _send_long(m, final)

        # сохраним ответ ассистента
        with SessionLocal() as db:
            _save_message(db, did, "assistant", final)

    except Exception:
        log.exception("on_text failed")
        await m.reply_text("⚠ Не удалось обработать сообщение. Попробуйте ещё раз.")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голос → Whisper → как on_text в активном диалоге."""
    m = update.effective_message or update.message
    voice = m.voice or m.audio
    if not voice:
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")
    try:
        file = await context.bot.get_file(voice.file_id)
        tmpdir = tempfile.mkdtemp(prefix="tg_voice_")
        ogg_path = os.path.join(tmpdir, "voice.ogg")  # важно расширение
        await file.download_to_drive(ogg_path)

        transcribe_model = getattr(settings, "openai_transcribe_model", None) or getattr(settings, "OPENAI_TRANSCRIBE_MODEL", "whisper-1")
        with open(ogg_path, "rb") as f:
            tr = _OA.audio.transcriptions.create(model=transcribe_model, file=f)
        recognized = (getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None) or "").strip()
        if not recognized:
            return await m.reply_text("⚠ Не удалось распознать речь. Скажите ещё раз, пожалуйста.")

        # прокинем в on_text
        update.effective_message.text = recognized
        return await on_text(update, context)

    except Exception:
        log.exception("on_voice failed")
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")

# ---- СЕРВИСНЫЕ ----
async def dbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
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
        os.environ["DATABASE_URL"] = getattr(settings,"database_url",None) or getattr(settings,"DATABASE_URL","")
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        await (update.effective_message or update.message).reply_text("✅ Миграции применены.")
    except Exception:
        log.exception("migrate failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка миграции.")

async def repair_schema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")
        await m.reply_text("🧰 Ремонт схемы начат...")
        created = []
        with SessionLocal() as db:
            def has(table: str) -> bool:
                return bool(db.execute(sa_text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar())

            # Базовые
            if not has("users"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS users (
                        id           BIGSERIAL PRIMARY KEY,
                        tg_user_id   BIGINT UNIQUE NOT NULL,
                        is_admin     BOOLEAN NOT NULL DEFAULT FALSE,
                        is_allowed   BOOLEAN NOT NULL DEFAULT TRUE,
                        lang         TEXT,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """))
                db.commit(); created.append("users")
            if not has("dialogs"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS dialogs (
                        id              BIGSERIAL PRIMARY KEY,
                        user_id         BIGINT NOT NULL,
                        title           TEXT,
                        style           VARCHAR(20) NOT NULL DEFAULT 'pro',
                        model           TEXT,
                        is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_message_at TIMESTAMPTZ
                    );
                """))
                db.commit(); created.append("dialogs")
            if not has("messages"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id         BIGSERIAL PRIMARY KEY,
                        dialog_id  BIGINT NOT NULL,
                        role       VARCHAR(20) NOT NULL,
                        content    TEXT NOT NULL,
                        tokens     INTEGER,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """))
                db.commit(); created.append("messages")

            # БЗ
            if not has("kb_documents"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS kb_documents (
                        id         BIGSERIAL PRIMARY KEY,
                        path       TEXT UNIQUE NOT NULL,
                        etag       TEXT,
                        mime       TEXT,
                        pages      INTEGER,
                        bytes      BIGINT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        is_active  BOOLEAN NOT NULL DEFAULT TRUE
                    );
                """))
                db.commit(); created.append("kb_documents")
            if not has("kb_chunks"):
                # если нет vector — всё равно создадим без ivfflat (минимум)
                try:
                    db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    db.commit()
                    has_vector = True
                except Exception:
                    db.rollback(); has_vector = False
                if has_vector:
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS kb_chunks (
                            id           BIGSERIAL PRIMARY KEY,
                            document_id  BIGINT NOT NULL,
                            chunk_index  INTEGER NOT NULL,
                            content      TEXT NOT NULL,
                            meta         JSON,
                            embedding    vector(3072)
                        );
                    """))
                    db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                    try:
                        db.execute(sa_text("CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops);"))
                    except Exception:
                        pass
                else:
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS kb_chunks (
                            id           BIGSERIAL PRIMARY KEY,
                            document_id  BIGINT NOT NULL,
                            chunk_index  INTEGER NOT NULL,
                            content      TEXT NOT NULL,
                            meta         JSON,
                            embedding    BYTEA
                        );
                    """))
                    db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                db.commit(); created.append("kb_chunks")
            if not has("dialog_kb_links"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS dialog_kb_links (
                        id          BIGSERIAL PRIMARY KEY,
                        dialog_id   BIGINT NOT NULL,
                        document_id BIGINT NOT NULL,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """))
                db.commit(); created.append("dialog_kb_links")
            if not has("pdf_passwords"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS pdf_passwords (
                        id          BIGSERIAL PRIMARY KEY,
                        dialog_id   BIGINT NOT NULL,
                        document_id BIGINT NOT NULL,
                        pwd_hash    TEXT,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """))
                db.commit(); created.append("pdf_passwords")
            if not has("audit_log"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id         BIGSERIAL PRIMARY KEY,
                        user_id    BIGINT,
                        action     TEXT,
                        meta       JSON,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                """))
                db.commit(); created.append("audit_log")

        await m.reply_text("✅ Ремонт завершён. Создано: " + (", ".join(created) if created else "ничего — всё уже было."))
    except Exception:
        log.exception("repair_schema failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка repair_schema")

async def pgvector_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
            avail = db.execute(sa_text("SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')")).scalar()
            installed = db.execute(sa_text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")).scalar()
        await (update.effective_message or update.message).reply_text(
            f"pgvector доступно: {'✅' if avail else '❌'}\n"
            f"pgvector установлено: {'✅' if installed else '❌'}"
        )
    except Exception:
        log.exception("pgvector_check failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка pgvector_check")

# ---------- СБОРКА ПРИЛОЖЕНИЯ ----------
def build_app() -> Application:
    _ensure_single_instance()
    token = getattr(settings, "telegram_bot_token", None) or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    app = ApplicationBuilder().token(token).post_init(_post_init).build()

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

    return app
