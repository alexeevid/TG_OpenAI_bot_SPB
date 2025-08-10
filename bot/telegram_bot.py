from __future__ import annotations
import tiktoken
from openai import OpenAI

import logging
from datetime import datetime
from io import BytesIO
# ==== KB RAG helpers (safe define if missing) ====
import json

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
from sqlalchemy import text as sa_text

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
        from sqlalchemy import text as sa_text
        from bot.db.session import engine
        need = True
        if not force:
            # Проверяем наличие ключевой таблицы
            with engine.connect() as conn:
                exists = conn.execute(sa_text("SELECT to_regclass('public.users')")).scalar()
                need = not bool(exists)

        if need:
            log.info("Auto-migrate: applying Alembic migrations...")
            # Настраиваем Alembic программно
            import os
            import time
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
    return db.execute(sa_text(sql), params).scalar()

def _exec_all(db, sql: str, **params):
    return db.execute(sa_text(sql), params).all()

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
    db.commit()
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
    db.commit()
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

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает voice/audio, распознаёт речь (OpenAI), затем тот же RAG-пайплайн, что и on_text.
    Делает подсказку на русский язык и имеет фолбэк-модель, если whisper вернул пусто.
    """
    from openai import OpenAI  # локальный импорт на всякий случай
    m = update.effective_message or update.message
    try:
        voice = getattr(m, "voice", None)
        audio = getattr(m, "audio", None)
        tg_file = voice or audio
        if not tg_file:
            return await m.reply_text("Голосовое не найдено.")

        # Ограничение по длительности (опционально)
        dur = int(getattr(tg_file, "duration", 0) or 0)
        if dur > 180:
            return await m.reply_text("Аудио длиннее 3 минут. Пришлите короче, пожалуйста.")

        # Скачиваем файл из Telegram
        file = await context.bot.get_file(tg_file.file_id)
        audio_bytes = await file.download_as_bytearray()

        buf = BytesIO(audio_bytes)
        # Имя файла помогает SDK определить формат; .oga / .ogg для voice из TG ок
        buf.name = "voice.ogg"

        client = OpenAI(api_key=settings.openai_api_key)

        # 1) Основная попытка — whisper-1 с подсказкой языка
        tr = client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="ru"
        )
        text = getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None)

        # 2) Фолбэк: если пусто — пробуем вторую модель распознавания
        if not (text or "").strip():
            buf.seek(0)
            tr2 = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=buf,
                language="ru"
            )
            text = getattr(tr2, "text", None) or (tr2.get("text") if isinstance(tr2, dict) else None)

        if not (text or "").strip():
            return await m.reply_text("Не удалось распознать речь, попробуйте ещё раз.")

        q = text.strip()

        # --- Далее как on_text: RAG-пайплайн ---
        with SessionLocal() as db:
            tg_id = update.effective_user.id
            user_id = _ensure_user(db, tg_id)
            dialog_id = _ensure_dialog(db, user_id)

            chunks = _retrieve_chunks(db, dialog_id, q, k=6)

            if chunks:
                ctx_blocks = [r["content"][:800] for r in chunks]
                prompt = _build_prompt(ctx_blocks, q)
            else:
                prompt = q

            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": "Ты полезный помощник."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
            )
            answer = resp.choices[0].message.content or "…"
            if chunks:
                answer += _format_citations(chunks)

        # Отдаём пользователю результат распознавания + ответ
        await m.reply_text(f"🗣️ Распознано:\n{q}")
        await m.reply_text(answer)

    except Exception:
        log.exception("on_voice failed")
        await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")


def ya_download(path: str) -> bytes:
    """
    Скачивает файл с Я.Диска по абсолютному пути (например, 'disk:/База Знаний/file.pdf').
    Возвращает бинарное содержимое файла.
    """
    import requests
    YA_API = "https://cloud-api.yandex.net/v1/disk"
    headers = {"Authorization": f"OAuth {settings.yandex_disk_token}"}

    # 1) получаем href для скачивания
    r = requests.get(
        f"{YA_API}/resources/download",
        headers=headers,
        params={"path": path},
        timeout=60,
    )
    r.raise_for_status()
    href = (r.json() or {}).get("href")
    if not href:
        raise RuntimeError("download href not returned by Yandex Disk")

    # 2) скачиваем сам файл
    f = requests.get(href, timeout=300)
    f.raise_for_status()
    return f.content

async def rag_selftest(update, context):
    from sqlalchemy import text as sa_text
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            t = db.execute(sa_text("SELECT pg_typeof(embedding)::text FROM kb_chunks LIMIT 1")).scalar()
            d = db.execute(sa_text("SELECT (embedding <=> embedding) FROM kb_chunks LIMIT 1")).scalar()
        await m.reply_text(f"pg_typeof(embedding) = {t}\n(embedding <=> embedding) = {d}")
    except Exception as e:
        log.exception("rag_selftest failed")
        await m.reply_text(f"❌ rag_selftest: {e}")

# ---- RAG helpers ----
from typing import List, Tuple

def _vec_literal(vec: list[float]) -> tuple[dict, str]:
    # подаём строку вида "[0.1,0.2,...]" в параметре q
    arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
    return {"q": arr}, "CAST(:q AS vector)"   # <- возвращаем params и SQL-выражение

def _embed_query(text: str) -> List[float]:
    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key)
    return client.embeddings.create(model=settings.embedding_model, input=[text]).data[0].embedding

def _retrieve_chunks(db, dialog_id: int, question: str, k: int = 6) -> List[dict]:
    # если столбец embedding не в vector-типе — просто вернём пусто (RAG отключится)
    kind = _kb_embedding_column_kind(db)
    if kind != "vector":
        return []

    q = _embed_query(question)
    params, qexpr = _vec_literal(q)
    
    sql = f"""
        SELECT c.content, c.meta, d.path
        FROM kb_chunks c
        JOIN kb_documents d    ON d.id = c.document_id AND d.is_active = TRUE
        JOIN dialog_kb_links l ON l.document_id = c.document_id
        WHERE l.dialog_id = :did
        ORDER BY c.embedding <=> {qexpr}
        LIMIT :k
    """
    p = {"did": dialog_id, "k": k}
    p.update(params)
    rows = db.execute(sa_text(sql), p).mappings().all()
    return [dict(r) for r in rows]

def _build_prompt(context_blocks: List[str], question: str) -> str:
    ctx = "\n\n".join(f"[{i+1}] {b}" for i, b in enumerate(context_blocks))
    return (
        "Ты отвечаешь кратко и по делу на основе контекста ниже. "
        "Если в контексте нет ответа, честно скажи об этом.\n\n"
        f"КОНТЕКСТ:\n{ctx}\n\n"
        f"ВОПРОС: {question}\nОТВЕТ:"
    )

def _format_citations(chunks: List[dict]) -> str:
    # Берём короткое имя файла
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

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    q = (m.text or "").strip()
    if not q:
        return

    try:
        with SessionLocal() as db:
            tg_id = update.effective_user.id
            user_id = _ensure_user(db, tg_id)
            dialog_id = _ensure_dialog(db, user_id)  # активный диалог

            # 1) пробуем достать контекст из подключённых документов
            chunks = _retrieve_chunks(db, dialog_id, q, k=6)

            from openai import OpenAI
            client = OpenAI(api_key=settings.openai_api_key)

            if chunks:
                ctx_blocks = [r["content"][:800] for r in chunks]  # аккуратно ограничим
                prompt = _build_prompt(ctx_blocks, q)
            else:
                # без RAG — обычный ответ модели
                prompt = q

            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role":"system","content":"Ты полезный помощник."},
                          {"role":"user","content": prompt}],
                temperature=0.2,
            )
            answer = resp.choices[0].message.content or "…"

            # добавим источники, если был контекст
            if chunks:
                answer += _format_citations(chunks)

            await m.reply_text(answer)
    except Exception:
        log.exception("on_text failed")
        await m.reply_text("⚠ Что-то пошло не так. Попробуйте ещё раз.")

# === DIAG: показать статус всех PDF на диске и что с ними при разборе ===
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

async def rag_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    q = " ".join(context.args) if context.args else ""
    if not q:
        return await m.reply_text("Напишите запрос: /rag_diag ваш вопрос")
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            rows = _retrieve_chunks(db, did, q, k=5)
            if not rows:
                return await m.reply_text("Ничего релевантного не нашли среди подключённых документов.")
            out = []
            for i, r in enumerate(rows, 1):
                path = (r.get("path") or (r.get("meta") or {}).get("path", "")).split("/")[-1]
                sample = (r["content"] or "")[:140].replace("\n", " ")
                out.append(f"[{i}] {path} — “{sample}…”")
            await m.reply_text("\n".join(out))
    except Exception:
        log.exception("rag_diag failed")
        await m.reply_text("⚠ rag_diag: ошибка. Смотри логи.")

# ---- PDF helpers ----
def _pdf_extract_text(pdf_bytes: bytes) -> tuple[str, int, bool]:
    """
    Возвращает (текст, pages, is_protected).
    1) PyMuPDF
    2) Fallback на pdfminer.six, если текст пустой и не защищён
    """
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
                text = doc.load_page(i).get_text("text") or ""
            except Exception:
                text = ""
            out.append(text)
        txt = "\n".join(out)

    # Fallback: если пусто, попробуем pdfminer
    if not txt.strip():
        try:
            from io import BytesIO
            from pdfminer.high_level import extract_text
            txt = extract_text(BytesIO(pdf_bytes)) or ""
        except Exception:
            pass

    return (txt, pages, False)


# 0) какого типа колонка embedding в kb_chunks: 'vector' | 'bytea' | 'none'
try:
    _kb_embedding_column_kind
except NameError:
    def _kb_embedding_column_kind(db) -> str:
        try:
            t = db.execute(sa_text("SELECT pg_typeof(embedding)::text FROM kb_chunks LIMIT 1")).scalar()
            if t:
                t = str(t).lower()
                if "vector" in t:
                    return "vector"
                if "bytea" in t:
                    return "bytea"
            # fallback через information_schema
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

# 1) upsert документа в kb_documents (возвращаем id)
try:
    _kb_upsert_document
except NameError:
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

# 2) очистка чанков по document_id
try:
    _kb_clear_chunks
except NameError:
    def _kb_clear_chunks(db, document_id: int):
        db.execute(sa_text("DELETE FROM kb_chunks WHERE document_id=:d"), {"d": document_id})
        db.commit()

# 3) загрузка файла с Я.Диска по пути
try:
    _ya_download
except NameError:
    def _chunk_text(text: str, max_tokens: int = 2000, overlap: int = 0):
        """Разбивает текст на куски по max_tokens для эмбеддингов с перекрытием overlap"""
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
    
        chunks = []
        i = 0
        while i < len(tokens):
            chunk_tokens = tokens[i:i+max_tokens]
            chunk_text = enc.decode(chunk_tokens)
            chunks.append(chunk_text.strip())
            if overlap > 0:
                i += max_tokens - overlap
            else:
                i += max_tokens
    
        return chunks

# 4) простая резка текста на чанки
try:
    _chunk_text
except NameError:
    def _chunk_text(text: str, max_tokens: int = 2000):
        """Разбивает текст на куски по max_tokens для эмбеддингов"""
        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
    
        chunks = []
        for i in range(0, len(tokens), max_tokens):
            chunk_tokens = tokens[i:i+max_tokens]
            chunk_text = enc.decode(chunk_tokens)
            chunks.append(chunk_text.strip())
    
        return chunks

# 5) эмбеддинги пачкой (OpenAI)
try:
    _get_embeddings
except NameError:
    def _get_embeddings(chunks: list[str]) -> list[list[float]]:
        """
        Считает эмбеддинги безопасно, батчами:
        - лимит по суммарным токенам ~250k на запрос (запас от 300k);
        - доп. лимит на размер батча по количеству элементов, чтобы не раздувать payload.
        """
        if not chunks:
            return []
    
        enc = tiktoken.get_encoding("cl100k_base")
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
    
        MAX_TOKENS_PER_REQ = 250_000   # запас от лимита 300k
        MAX_ITEMS_PER_REQ  = 128       # на всякий случай ограничим и по числу строк
    
        out: list[list[float]] = []
        batch: list[str] = []
        batch_tok_sum = 0
    
        def flush_batch():
            nonlocal out, batch, batch_tok_sum
            if not batch:
                return
            resp = client.embeddings.create(model=settings.embedding_model, input=batch)
            data = getattr(resp, "data", None) or resp.get("data", [])
            # order гарантирован, просто дописываем
            out.extend([item.embedding for item in data])
            batch = []
            batch_tok_sum = 0
    
        for ch in chunks:
            t = len(enc.encode(ch or ""))
            # если одиночный чанк вдруг больше лимита — уже порезан ранее; но на всякий случай:
            if t > MAX_TOKENS_PER_REQ:
                # дополнительная защита: дорезать на подчанки по 2000 токенов
                subchunks = []
                toks = enc.encode(ch or "")
                for i in range(0, len(toks), 2000):
                    subchunks.append(enc.decode(toks[i:i+2000]))
                # рекурсивно прогоняем подчанки тем же механизмом
                out.extend(_get_embeddings(subchunks))
                continue
    
            # если текущий не влезает в батч — шлём то, что накоплено
            if batch and (batch_tok_sum + t > MAX_TOKENS_PER_REQ or len(batch) >= MAX_ITEMS_PER_REQ):
                flush_batch()
    
            batch.append(ch)
            batch_tok_sum += t
    
        flush_batch()
        return out

# 6) SQL-фрагменты для вставки эмбеддингов
try:
    _format_vector_sql
except NameError:
    def _format_vector_sql(vec: list[float]) -> tuple[str, dict]:
        arr = "[" + ",".join(f"{x:.6f}" for x in (vec or [])) + "]"
        return " CAST(:emb AS vector) ", {"emb": arr}

try:
    _format_bytea_sql
except NameError:
    def _format_bytea_sql(vec: list[float]) -> tuple[str, dict]:
        try:
            import struct
            from psycopg2 import Binary
            b = struct.pack(f"{len(vec)}f", *vec) if vec else b""
            return " :emb ", {"emb": Binary(b)}
        except Exception:
            return " NULL ", {}
# ==== /KB RAG helpers ====

# --- Fallback листинг Яндекс.Диска (если нет собственного хелпера) ---
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
            "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.type,_embedded.items.mime_type,_embedded.items.size,_embedded.items.md5",
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

# Синхронизировать только PDF (без пароля). Защищённые PDF регистрируем, но не индексируем.

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
            emb_kind = _kb_embedding_column_kind(db)  # 'vector' | 'bytea' | 'none'

            # Деактивируем PDF, которых нет на диске
            present = { (f.get("path") or f.get("name")) for f in files if (f.get("path") or f.get("name")) }
            rows = db.execute(sa_text("SELECT id, path, is_active FROM kb_documents WHERE mime LIKE 'application/pdf%'")).mappings().all()
            for r in rows:
                if r["path"] not in present and r["is_active"]:
                    db.execute(sa_text("UPDATE kb_documents SET is_active=FALSE, updated_at=now() WHERE id=:id"), {"id": r["id"]})
            db.commit()

            for it in files:
                path  = it.get("path") or it.get("name")
                mime  = it.get("mime_type") or "application/pdf"
                size  = int(it.get("size") or 0)
                etag  = it.get("md5") or ""
                if not path:
                    continue

                doc_id = _kb_upsert_document(db, path=path, mime=mime, size=size, etag=etag)

                # Скачиваем
                try:
                    blob = ya_download(path)
                except Exception as e:
                    log.exception("pdf download failed: %s (%s)", path, e)
                    continue

                # Парсим
                try:
                    txt, pages, is_prot = _pdf_extract_text(blob)
                except Exception as e:
                    log.exception("pdf parse failed: %s (%s)", path, e)
                    continue

                _kb_update_pages(db, doc_id, pages if pages else None)

                # Защищённый или пустой PDF — не индексируем
                if is_prot or not txt.strip():
                    log.info("pdf skipped (protected or empty): %s", path)
                    continue

                # Переиндексация целиком для документа
                _kb_clear_chunks(db, doc_id)

                chunks = _chunk_text(txt, settings.chunk_size, settings.chunk_overlap)
                embs = _get_embeddings(chunks) if emb_kind in ("vector","bytea") else [[] for _ in chunks]

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

async def kb_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Временная заглушка, чтобы не падал импорт. Полная синхронизация у нас через /kb_sync_pdf."""
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.effective_message or update.message).reply_text("Только для админа.")
        return await (update.effective_message or update.message).reply_text(
            "ℹ Используйте /kb_sync_pdf — он выполняет актуальную синхронизацию PDF и деактивацию удалённых файлов."
        )
    except Exception:
        log.exception("kb_sync (stub) failed")

async def kb_chunks_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")

        from sqlalchemy import text as sa_text
        notes = []

        with SessionLocal() as db:
            # 0) vector на всякий случай (без паники при ошибке)
            try:
                db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector;"))
                db.commit()
            except Exception:
                db.rollback()
                notes.append("[warn] CREATE EXTENSION vector failed")

            # 1) Создать таблицу (если vector есть — с vector(3072), иначе fallback BYTEA)
            has_vector_type = False
            try:
                has_vector_type = db.execute(sa_text(
                    "SELECT EXISTS(SELECT 1 FROM pg_type WHERE typname='vector')"
                )).scalar()
            except Exception:
                pass

            try:
                if has_vector_type:
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
                    notes.append("[info] fallback: embedding BYTEA (нет типа vector)")
                db.commit()
            except Exception as e:
                db.rollback()
                raise RuntimeError(f"CREATE TABLE failed: {e}")

            # 2) Простейшие индексы (без ivfflat) — отдельная транзакция
            try:
                db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_doc_chunk ON kb_chunks(document_id, chunk_index);"))
                db.commit()
            except Exception as e:
                db.rollback()
                notes.append(f"[warn] create simple indexes failed: {e}")

            # 3) Внешний ключ на kb_documents — отдельная транзакция
            try:
                db.execute(sa_text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                           SELECT 1 FROM pg_constraint WHERE conname='fk_kb_chunks_docs'
                        ) THEN
                           ALTER TABLE kb_chunks
                           ADD CONSTRAINT fk_kb_chunks_docs
                           FOREIGN KEY (document_id) REFERENCES kb_documents(id) ON DELETE CASCADE;
                        END IF;
                    END $$;
                """))
                db.commit()
            except Exception as e:
                db.rollback()
                notes.append(f"[warn] add FK failed: {e}")

        await m.reply_text("✅ kb_chunks создана и подготовлена (без ivfflat).\n" + ("\n".join(notes) if notes else ""))
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=3)
        log.exception("kb_chunks_force failed")
        await m.reply_text(f"⚠ kb_chunks_force: {e}\n{tb}")

# Доведём kb_chunks: уберём ivfflat, добавим FK и тех.индексы
async def kb_chunks_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")
        from sqlalchemy import text as sa_text
        with SessionLocal() as db:
            # удалить ivfflat-индекс если он успел создаться частично (на всякий)
            try:
                db.execute(sa_text("DROP INDEX IF EXISTS kb_chunks_embedding_idx;"))
                db.commit()
            except Exception:
                db.rollback()
            # минимальные индексы для скорости по документам
            db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
            db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_doc_chunk ON kb_chunks(document_id, chunk_index);"))
            # добавить FK в отдельной транзакции
            try:
                db.execute(sa_text("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_kb_chunks_docs') THEN
                            ALTER TABLE kb_chunks
                            ADD CONSTRAINT fk_kb_chunks_docs
                            FOREIGN KEY (document_id) REFERENCES kb_documents(id) ON DELETE CASCADE;
                        END IF;
                    END $$;
                """))
            except Exception:
                pass
            db.commit()
        await m.reply_text("✅ kb_chunks починена: без ivfflat, с FK и индексами.")
    except Exception:
        log.exception("kb_chunks_fix failed")
        await m.reply_text("⚠ Не удалось починить kb_chunks. Смотри логи.")

# Создать kb_chunks надёжно: с vector, а при ошибке — fallback без vector
async def kb_chunks_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")

        from sqlalchemy import text as sa_text
        created_note = ""
        with SessionLocal() as db:
            # Диагностика окружения
            search_path = db.execute(sa_text("SHOW search_path")).scalar()
            has_tbl = db.execute(sa_text("SELECT to_regclass('public.kb_chunks') IS NOT NULL")).scalar()
            has_vector_ext = db.execute(sa_text(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"
            )).scalar()
            has_vector_type = db.execute(sa_text(
                "SELECT EXISTS(SELECT 1 FROM pg_type WHERE typname='vector')"
            )).scalar()

            if has_tbl:
                return await m.reply_text("✅ kb_chunks уже существует.")

            # На всякий случай — расширение
            if not has_vector_ext:
                try:
                    db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    db.commit()
                    has_vector_ext = True
                except Exception as e:
                    db.rollback()
                    # не падаем — попробуем fallback
                    created_note += f"[warn] CREATE EXTENSION failed: {e}\n"

            # Обновим наличие типа
            if not has_vector_type:
                has_vector_type = db.execute(sa_text(
                    "SELECT EXISTS(SELECT 1 FROM pg_type WHERE typname='vector')"
                )).scalar()

            try:
                if has_vector_type:
                    # Основной вариант: с vector(3072)
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
                    # Индексы и FK (best-effort)
                    try:
                        db.execute(sa_text(
                            "CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                        db.execute(sa_text("""
                            CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
                            ON kb_chunks USING ivfflat (embedding vector_cosine_ops);
                        """))
                    except Exception as e:
                        created_note += f"[warn] index create failed: {e}\n"
                    try:
                        db.execute(sa_text("""
                            DO $$
                            BEGIN
                                IF NOT EXISTS (
                                   SELECT 1 FROM pg_constraint WHERE conname = 'fk_kb_chunks_docs'
                                ) THEN
                                   ALTER TABLE kb_chunks
                                   ADD CONSTRAINT fk_kb_chunks_docs
                                   FOREIGN KEY (document_id)
                                   REFERENCES kb_documents(id) ON DELETE CASCADE;
                                END IF;
                            END $$;
                        """))
                    except Exception as e:
                        created_note += f"[warn] FK create failed: {e}\n"

                    db.commit()
                    return await m.reply_text(
                        "✅ kb_chunks создана (vector). "
                        f"\nsearch_path={search_path}\n{created_note or ''}".strip()
                    )

                # Fallback: без vector — embedding как BYTEA, без ivfflat
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
                try:
                    db.execute(sa_text(
                        "CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                except Exception as e:
                    created_note += f"[warn] fallback index failed: {e}\n"
                try:
                    db.execute(sa_text("""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                               SELECT 1 FROM pg_constraint WHERE conname = 'fk_kb_chunks_docs'
                            ) THEN
                               ALTER TABLE kb_chunks
                               ADD CONSTRAINT fk_kb_chunks_docs
                               FOREIGN KEY (document_id)
                               REFERENCES kb_documents(id) ON DELETE CASCADE;
                            END IF;
                        END $$;
                    """))
                except Exception as e:
                    created_note += f"[warn] fallback FK failed: {e}\n"

                db.commit()
                return await m.reply_text(
                    "✅ kb_chunks создана (fallback без vector). "
                    f"\nsearch_path={search_path}\n{created_note or ''}".strip()
                )

            except Exception as e:
                db.rollback()
                raise e

    except Exception as e:
        log.exception("kb_chunks_create failed")
        await m.reply_text(f"⚠ Не удалось создать kb_chunks: {e}")

# создать новый диалог вручную
async def dialog_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = update.effective_message or update.message
        tg_id = update.effective_user.id
        with SessionLocal() as db:
            uid = _ensure_user(db, tg_id)
            did = _exec_scalar(
                db,
                """
                INSERT INTO dialogs (user_id, title, style, model, is_deleted)
                VALUES (:u, :t, 'expert', :m, FALSE)
                RETURNING id
                """,
                u=uid, t=datetime.now().strftime("%Y-%m-%d | диалог"), m=settings.openai_model
            )
        db.commit()
        await m.reply_text(f"✅ Создан диалог #{did}")
    except Exception:
        log.exception("dialog_new failed")
        await (update.effective_message or update.message).reply_text("⚠ Не удалось создать диалог.")

async def pgvector_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
            avail = db.execute(sa_text(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector')"
            )).scalar()
            installed = db.execute(sa_text(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"
            )).scalar()
        await (update.effective_message or update.message).reply_text(
            f"pgvector доступно: {'✅' if avail else '❌'}\n"
            f"pgvector установлено: {'✅' if installed else '❌'}"
        )
    except Exception:
        log.exception("pgvector_check failed")
        await (update.effective_message or update.message).reply_text("⚠ Ошибка pgvector_check. Смотри логи.")

async def repair_schema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Чинит схему по шагам и фиксирует прогресс после КАЖДОЙ таблицы.
    Даже если на kb_* упадёт, базовые таблицы users/dialogs/messages останутся.
    """
    m = update.effective_message or update.message
    try:
        if not _is_admin(update.effective_user.id):
            return await m.reply_text("Только для админа.")

        await m.reply_text("🧰 Ремонт схемы начат. Пишу прогресс в логи...")

        from sqlalchemy import text as sa_text
        created = []
        with SessionLocal() as db:

            def has(table: str) -> bool:
                return bool(db.execute(sa_text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar())

            # 0) vector extension — отдельно и без паники
            try:
                db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector;"))
                db.commit()
                log.info("repair: extension vector OK (или уже было)")
            except Exception:
                db.rollback()
                log.exception("repair: CREATE EXTENSION vector failed — продолжу без него")

            # 1) USERS — СНАЧАЛА БАЗА
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
                db.commit(); created.append("users"); log.info("repair: created users")

            # 2) DIALOGS
            if not has("dialogs"):
                db.execute(sa_text("""
                    CREATE TABLE IF NOT EXISTS dialogs (
                        id              BIGSERIAL PRIMARY KEY,
                        user_id         BIGINT NOT NULL,
                        title           TEXT,
                        style           VARCHAR(20) NOT NULL DEFAULT 'expert',
                        model           TEXT,
                        is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_message_at TIMESTAMPTZ
                    );
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

            # 3) MESSAGES
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

            # --- Блок БЗ: делаем best-effort, каждый шаг в своей транзакции ---

            # 4) KB_DOCUMENTS
            try:
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
                    db.commit(); created.append("kb_documents"); log.info("repair: created kb_documents")
            except Exception:
                db.rollback(); log.exception("repair: create kb_documents failed (пропускаю)")

            # 5) KB_CHUNKS
            try:
                if not has("kb_chunks"):
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
                    try:
                        db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_kb_chunks_document_id ON kb_chunks(document_id);"))
                        db.execute(sa_text("""
                            CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
                            ON kb_chunks USING ivfflat (embedding vector_cosine_ops);
                        """))
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
                db.rollback(); log.exception("repair: create kb_chunks failed (возможно, нет расширения vector)")

            # 6) DIALOG_KB_LINKS
            try:
                if not has("dialog_kb_links"):
                    db.execute(sa_text("""
                        CREATE TABLE IF NOT EXISTS dialog_kb_links (
                            id          BIGSERIAL PRIMARY KEY,
                            dialog_id   BIGINT NOT NULL,
                            document_id BIGINT NOT NULL,
                            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                        );
                    """))
                    db.commit(); created.append("dialog_kb_links"); log.info("repair: created dialog_kb_links")
            except Exception:
                db.rollback(); log.exception("repair: create dialog_kb_links failed")

            # 7) PDF_PASSWORDS
            try:
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
                    db.commit(); created.append("pdf_passwords"); log.info("repair: created pdf_passwords")
            except Exception:
                db.rollback(); log.exception("repair: create pdf_passwords failed")

            # 8) AUDIT_LOG
            try:
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
                    db.commit(); created.append("audit_log"); log.info("repair: created audit_log")
            except Exception:
                db.rollback(); log.exception("repair: create audit_log failed")

        await m.reply_text("✅ Готово. Создано: " + (", ".join(created) if created else "ничего (всё уже было)"))
    except Exception:
        log.exception("repair_schema failed (outer)")
        await m.reply_text("⚠ Ошибка repair_schema. Смотри логи.")

# Проверка наличия таблиц в БД
async def dbcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with SessionLocal() as db:
            rows = db.execute(sa_text("""
                select 'users' as t, to_regclass('public.users') is not null as ok
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
        await (update.effective_message or update.message).reply_text("⚠ Ошибка dbcheck. Смотри логи.")

# Принудительный прогон миграций Alembic (только для админа)
async def migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.effective_message or update.message).reply_text("Только для админа.")
        await (update.effective_message or update.message).reply_text("🔧 Запускаю миграции...")
        # Программный вызов Alembic
        import os
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
    try:
        with SessionLocal() as db:
            db.execute(sa_text("SELECT 1"))
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
                db.execute(sa_text("UPDATE dialogs SET is_deleted=TRUE WHERE id=:d"), {"d": dlg_id})
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
                db.execute(sa_text("UPDATE dialogs SET title=:t WHERE id=:d"), {"t": new_title, "d": dlg_id})
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
                    db.execute(sa_text("DELETE FROM dialog_kb_links WHERE id=:i"), {"i": exist})
                else:
                    db.execute(sa_text(
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
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dialog", dialog))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("model", model))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("dialogs", dialogs))
    app.add_handler(CallbackQueryHandler(dialog_cb, pattern=r"^dlg:"))
    app.add_handler(CommandHandler("repair_schema", repair_schema))
    app.add_handler(CommandHandler("dbcheck", dbcheck))
    app.add_handler(CommandHandler("migrate", migrate))
    app.add_handler(CommandHandler("kb", kb))
    app.add_handler(CallbackQueryHandler(kb_cb, pattern=r"^kb:"))
    app.add_handler(CommandHandler("dialog_new", dialog_new))
    app.add_handler(CommandHandler("pgvector_check", pgvector_check))
    app.add_handler(CommandHandler("kb_chunks_create", kb_chunks_create))
    app.add_handler(CommandHandler("kb_chunks_fix", kb_chunks_fix))
    app.add_handler(CommandHandler("kb_chunks_force", kb_chunks_force))
    app.add_handler(CommandHandler("kb_sync", kb_sync))
    app.add_handler(CommandHandler("kb_sync_pdf", kb_sync_pdf))
    app.add_handler(CommandHandler("rag_diag", rag_diag))
    app.add_handler(CommandHandler("rag_selftest", rag_selftest))
    app.add_handler(CommandHandler("kb_pdf_diag", kb_pdf_diag))

    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
