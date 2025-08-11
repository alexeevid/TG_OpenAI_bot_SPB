from __future__ import annotations
import tiktoken
from openai import OpenAI
from io import BytesIO

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

async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not _is_admin(update.effective_user.id):
            return await (update.message or update.effective_message).reply_text("⛔ Доступ запрещён (нужно быть админом).")
        args = (update.message.text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            return await (update.message or update.effective_message).reply_text("Использование: /grant <tg_id>")
        target = int(args[1])
        with SessionLocal() as db:
            uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg", tg=target)
            if not uid:
                uid = _exec_scalar(db, "INSERT INTO users (tg_user_id, is_admin, is_allowed, lang) VALUES (:tg,FALSE,TRUE,'ru') RETURNING id", tg=target)
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
# ---------- commands ----------
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
        "/web <запрос> — (заглушка) веб-поиск\n"
        "/reset — сброс контекста активного диалога\n"
        "/whoami — мои права\n"
    )

async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    query = " ".join(context.args or [])
    if not query:
        return await m.reply_text("Использование: /web <запрос>")
    await m.reply_text(
        "🔎 Веб-поиск пока отключён в этой сборке (ключи внешнего поиска не заданы).\n"
        "Я могу ответить своими знаниями или попробовать найти в БЗ через /kb."
    )

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        voice = getattr(m, "voice", None)
        audio = getattr(m, "audio", None)
        tg_file = voice or audio
        if not tg_file:
            return await m.reply_text("Голосовое не найдено.")
        fobj = await tg_file.get_file()
        bio = BytesIO()
        await fobj.download_to_memory(out=bio)
        bio.seek(0)
        # ВАЖНО: у BytesIO должно быть имя, иначе OpenAI не распознаёт формат
        try:
            bio.name = "voice.ogg"  # type: ignore[attr-defined]
        except Exception:
            pass

        client = OpenAI(api_key=settings.openai_api_key)
        try:
            tr = client.audio.transcriptions.create(model="whisper-1", file=bio, language="ru")
            text = getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None) or ""
        except Exception:
            # запасной вариант
            bio.seek(0)
            tr = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=bio, language="ru")
            text = getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None) or ""

        if not text.strip():
            return await m.reply_text("Не удалось распознать речь, попробуйте ещё раз.")

        q = text.strip()
        low = q.lower()
        if low.startswith("нарисуй") or low.startswith("сгенерируй картинку"):
            prompt = q.split(":", 1)[1].strip() if ":" in q else q.replace("Нарисуй", "").replace("нарисуй", "").replace("Сгенерируй картинку", "").strip()
            if prompt:
                from bot.openai_helper import generate_image_bytes
                img_bytes, final_prompt = await generate_image_bytes(prompt)
                return await m.reply_photo(photo=img_bytes, caption=f"🖼️ Сгенерировано по голосовой команде\nPrompt → {final_prompt}")

        # обычный RAG-ответ
        from bot.openai_helper import chat as ai_chat
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            dia_model = row[0] if row and row[0] else settings.openai_model
            dia_style = row[1] if row and row[1] else "pro"
            chunks = _retrieve_chunks(db, did, q, k=6)
            ctx_blocks = [c.get("content", "") for c in chunks] if chunks else []
        prompt = _build_prompt_with_style(ctx_blocks, q, dia_style) if ctx_blocks else q
        answer = await ai_chat([{"role":"system","content":"RAG assistant"},{"role":"user","content":prompt}], model=dia_model, max_tokens=800)
        if chunks:
            answer += _format_citations(chunks)

        # сохраняем историю
        try:
            with SessionLocal() as db:
                uid = _ensure_user(db, update.effective_user.id)
                did = _ensure_dialog(db, uid)
                db.execute(sa_text("INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'user',:c,now())"),
                           {"d": did, "c": q})
                db.execute(sa_text("INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'assistant',:c,now())"),
                           {"d": did, "c": answer})
                db.execute(sa_text("UPDATE dialogs SET last_message_at=now() WHERE id=:d"), {"d": did})
                db.commit()
        except Exception:
            log.exception("save voice messages failed")

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

_STYLE_EXAMPLES = {
    "pro":    "Кратко, по шагам, чек-лист. Без воды. Пример: «Шаги 1–5, риски, KPI, дедлайны».",
    "expert": "Глубоко и обстоятельно: причины/следствия, альтернативы, ссылки. Пример: «Начнём с контекста и ограничений…».",
    "user":   "Просто, понятным языком, с метафорами и примерами из жизни.",
    "ceo":    "С точки зрения бизнеса: ценность/стоимость, риски, сроки, решения, варианты и trade-offs.",
}

def _build_prompt_with_style(ctx_blocks: List[str], user_q: str, dialog_style: str) -> str:
    style_map = {
        "pro":   "Профессионал: максимально ёмко и по делу, шаги и чек-лист.",
        "expert":"Эксперт: подробно, причины/следствия, альтернативы, выводы. Цитаты из источников только в конце.",
        "user":  "Пользователь: простыми словами, примеры и аналогии.",
        "ceo":   "CEO: бизнес-ценность, ROI, риски, решения и компромиссы.",
    }
    style_line = style_map.get(dialog_style or "pro", style_map["pro"])
    header = (
        "Ты — аккуратный ассистент. Используй контекст БЗ, но не ограничивайся цитатами: "
        "синтезируй цельный ответ в выбранном стиле. Если уверенности нет — уточни."
    )
    ctx = "\n\n".join([f"[Фрагмент #{i+1}]\n{t}" for i, t in enumerate(ctx_blocks)])
    return f"{header}\nСтиль: {style_line}\n\nКонтекст:\n{ctx}\n\nВопрос: {user_q}"

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
    # перехват ввода для переименования
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
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            row = db.execute(sa_text("SELECT model, style FROM dialogs WHERE id=:d"), {"d": did}).first()
            dia_model = row[0] if row and row[0] else settings.openai_model
            dia_style = row[1] if row and row[1] else "pro"
            chunks = _retrieve_chunks(db, did, q, k=6)
            ctx_blocks = [r["content"][:900] for r in chunks] if chunks else []

        from bot.openai_helper import chat as ai_chat
        prompt = _build_prompt_with_style(ctx_blocks, q, dia_style) if ctx_blocks else q
        answer = await ai_chat(
            [{"role": "system", "content": "RAG assistant"}, {"role": "user", "content": prompt}],
            model=dia_model,
            max_tokens=900
        )
        if chunks:
            answer += _format_citations(chunks)

        # сохраняем историю
        try:
            with SessionLocal() as db:
                uid = _ensure_user(db, update.effective_user.id)
                did = _ensure_dialog(db, uid)
                db.execute(sa_text("INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'user',:c,now())"),
                           {"d": did, "c": q})
                db.execute(sa_text("INSERT INTO messages (dialog_id, role, content, created_at) VALUES (:d,'assistant',:c,now())"),
                           {"d": did, "c": answer})
                db.execute(sa_text("UPDATE dialogs SET last_message_at=now() WHERE id=:d"), {"d": did})
                db.commit()
        except Exception:
            log.exception("save messages failed")

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
    # запуск полной индексации PDF (деактивация удалённых и т.д.)
    return await kb_sync_pdf(update, context)

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
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            row = db.execute(sa_text("""
                SELECT d.id, d.title, d.model, d.style, d.created_at, d.last_message_at
                FROM dialogs d WHERE d.id=:d
            """), {"d": did}).first()
            links = db.execute(sa_text("""
                SELECT kd.path FROM dialog_kb_links l
                JOIN kb_documents kd ON kd.id = l.document_id
                WHERE l.dialog_id=:d
                ORDER BY kd.path
            """), {"d": did}).fetchall()
            total_dialogs = _exec_scalar(db, "SELECT count(*) FROM dialogs WHERE user_id=:u AND is_deleted=FALSE", u=uid) or 0
            total_msgs = _exec_scalar(db, "SELECT count(*) FROM messages WHERE dialog_id=:d", d=did) or 0

        title = row[1] or ""
        model = row[2] or settings.openai_model
        style = row[3] or "-"
        created = row[4].strftime("%Y-%m-%d %H:%M") if row and row[4] else "-"
        updated = row[5].strftime("%Y-%m-%d %H:%M") if row and row[5] else "-"
        docs = [r[0] for r in links] if links else []

        lines = [
            f"Диалог: {row[0]} — {title}",
            f"Модель: {model} | Стиль: {style}",
            f"Создан: {created} | Изменён: {updated}",
            f"Подключённые документы ({len(docs)}):",
            *[f"• {p}" for p in docs],
            "",
            f"Всего твоих диалогов: {total_dialogs} | Сообщений в этом диалоге: {total_msgs}",
        ]
        await m.reply_text("\n".join(lines))
    except Exception:
        log.exception("stats failed")
        await m.reply_text("⚠ Ошибка /stats")

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data.startswith("dlg:open:"):
            dlg_id = int(data.split(":")[-1])
            await q.edit_message_text(f"Открыт диалог #{dlg_id}")
            return

        if data == "dlg:nop" or data.startswith("dlg:page:"):
            # просто перерисуем список
            return await dialogs(update, context)

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
    nav.append(InlineKeyboardButton(f"Страница {page}/{pages}", callback_data="kb:nop"))
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
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            did = _ensure_dialog(db, uid)
            # чистим всё, что относится к текущему диалогу
            db.execute(sa_text("DELETE FROM messages WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("DELETE FROM dialog_kb_links WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("DELETE FROM pdf_passwords WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("UPDATE dialogs SET last_message_at=NULL WHERE id=:d"), {"d": did})
            db.commit()
        await m.reply_text("Контекст текущего диалога очищен.")
    except Exception:
        log.exception("reset failed")
        await m.reply_text("⚠ Ошибка /reset")

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

# === /model: inline selector of available OpenAI models ===
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

async def model_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        models = client.models.list()
        ids = [it.id for it in getattr(models, "data", [])]
        ids = sorted(set(ids), key=_model_score, reverse=True)
        top10 = ids[:10]
        buttons = [[InlineKeyboardButton(mid, callback_data=f"model:set:{mid}")] for mid in top10]
        buttons.append([InlineKeyboardButton("Показать ещё", callback_data="model:more:1")])
        buttons.append([InlineKeyboardButton("Закрыть", callback_data="model:close")])
        await m.reply_text("Выберите модель для текущего диалога:", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        log.exception("model_menu failed")
        await m.reply_text("⚠ Не удалось получить список моделей")

def _send_model_page(all_ids, page: int, qmsg):
    PAGE = 10
    pages = max(1, (len(all_ids) + PAGE - 1) // PAGE)
    page = max(1, min(page, pages))
    beg = (page-1) * PAGE
    chunk = all_ids[beg:beg+PAGE]
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

async def model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data == "model:close" or data == "model:nop":
            try:
                await q.delete_message()
            except Exception:
                pass
            return

        if data.startswith("model:more:"):
            page = int(data.split(":")[-1])
            client = OpenAI(api_key=settings.openai_api_key)
            models = client.models.list()
            ids = [it.id for it in getattr(models, "data", [])]
            ids = sorted(set(ids), key=_model_score, reverse=True)[10:]  # всё, кроме ТОП-10
            await q.edit_message_reply_markup(reply_markup=_send_model_page(ids, page, q))
            return

        if data.startswith("model:set:"):
            mid = data.split(":", 2)[-1]
            # пробуем простое эхо-обращение к модели, если упадёт — не сохраняем
            ok = True
            try:
                client = OpenAI(api_key=settings.openai_api_key)
                client.chat.completions.create(
                    model=mid,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1
                )
            except Exception:
                ok = False
            if not ok:
                await q.edit_message_text(f"❌ Не удалось выбрать модель «{mid}». Попробуйте другую.")
                return
            with SessionLocal() as db:
                uid = _ensure_user(db, update.effective_user.id)
                did = _ensure_dialog(db, uid)
                db.execute(sa_text("UPDATE dialogs SET model=:m WHERE id=:d"), {"m": mid, "d": did})
                db.commit()
            await q.edit_message_text(f"✅ Установлена модель: {mid}")
            return
    except Exception:
        log.exception("model_cb failed")
        try:
            await q.message.reply_text("⚠ Ошибка выбора модели")
        except Exception:
            pass

async def mode_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Профессионал", callback_data="mode:set:pro")],
        [InlineKeyboardButton("Эксперт", callback_data="mode:set:expert")],
        [InlineKeyboardButton("Пользователь", callback_data="mode:set:user")],
        [InlineKeyboardButton("СЕО", callback_data="mode:set:ceo")],
        [InlineKeyboardButton("Закрыть", callback_data="mode:close")],
    ])
    await m.reply_text("Выберите стиль ответа для текущего диалога:", reply_markup=kb)

async def mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
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
        if style not in ("ceo","expert","pro","user"):
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

async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    q = (m.text or "").split(maxsplit=1)
    if len(q) < 2:
        return await m.reply_text("Использование: /img <описание>")
    try:
        from bot.openai_helper import generate_image_bytes
        content, final_prompt = await generate_image_bytes(q[1])
        await m.reply_photo(photo=content, caption=f"🖼️ Сгенерировано DALL·E 3\nPrompt → {final_prompt}")
    except Exception:
        log.exception("img failed")
        await m.reply_text("⚠ Не удалось сгенерировать изображение.")

DIALOGS_PAGE_SIZE = 6

async def dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            uid = _ensure_user(db, update.effective_user.id)
            ds = _exec_all(db, """
                SELECT id, COALESCE(title, '') FROM dialogs
                WHERE user_id=:u AND is_deleted=FALSE
                ORDER BY created_at DESC
            """, u=uid)
        total = len(ds)
        page = 1
        pages = max(1, (total + DIALOGS_PAGE_SIZE - 1) // DIALOGS_PAGE_SIZE)
        beg = (page-1) * DIALOGS_PAGE_SIZE
        chunk = ds[beg:beg + DIALOGS_PAGE_SIZE]

        rows = []
        for did, title in chunk:
            name = title or f"Диалог #{did}"
            rows.append([
                InlineKeyboardButton(name[:30] + ("…" if len(name) > 30 else ""), callback_data=f"dlg:open:{did}"),
                InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{did}"),
                InlineKeyboardButton("📤", callback_data=f"dlg:export:{did}"),
                InlineKeyboardButton("🗑️", callback_data=f"dlg:delete:{did}"),
            ])

        nav = []
        if pages > 1:
            nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"dlg:page:{page+1}"))
        rows.append(nav or [InlineKeyboardButton(" ", callback_data="dlg:nop")])

        await m.reply_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("dialogs failed")
        await m.reply_text("⚠ Ошибка /dialogs")

async def dialog_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data.startswith("dlg:page:"):
            page = int(data.split(":")[-1])
            with SessionLocal() as db:
                uid = _ensure_user(db, update.effective_user.id)
                ds = _exec_all(db, """
                    SELECT id, COALESCE(title, '') FROM dialogs
                    WHERE user_id=:u AND is_deleted=FALSE
                    ORDER BY created_at DESC
                """, u=uid)
            total = len(ds)
            pages = max(1, (total + DIALOGS_PAGE_SIZE - 1) // DIALOGS_PAGE_SIZE)
            page = max(1, min(page, pages))
            beg = (page-1) * DIALOGS_PAGE_SIZE
            chunk = ds[beg:beg + DIALOGS_PAGE_SIZE]

            rows = []
            for did, title in chunk:
                name = title or f"Диалог #{did}"
                rows.append([
                    InlineKeyboardButton(name[:30] + ("…" if len(name) > 30 else ""), callback_data=f"dlg:open:{did}"),
                    InlineKeyboardButton("✏️", callback_data=f"dlg:rename:{did}"),
                    InlineKeyboardButton("📤", callback_data=f"dlg:export:{did}"),
                    InlineKeyboardButton("🗑️", callback_data=f"dlg:delete:{did}"),
                ])
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("« Назад", callback_data=f"dlg:page:{page-1}"))
            nav.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="dlg:nop"))
            if page < pages:
                nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"dlg:page:{page+1}"))
            rows.append(nav)

            await q.edit_message_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(rows))
            return

        if data.startswith("dlg:open:"):
            dlg_id = int(data.split(":")[-1])
            # делаем выбранный диалог активным (поскольку активный берётся как «последний созданный»)
            with SessionLocal() as db:
                db.execute(sa_text("UPDATE dialogs SET created_at = now() WHERE id=:d"), {"d": dlg_id})
                db.commit()
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
                msgs = _exec_all(db, """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE dialog_id=:d
                    ORDER BY created_at
                """, d=dlg_id)
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
        
def build_app() -> Application:
    apply_migrations_if_needed()
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CallbackQueryHandler(model_cb, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(mode_cb, pattern=r"^mode:"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("model", model_menu))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("mode", mode_menu))
    app.add_handler(CommandHandler("dialogs", dialogs))
    app.add_handler(CommandHandler("img", cmd_img))
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
    app.add_handler(CommandHandler("web", cmd_web))

    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
