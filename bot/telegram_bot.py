from __future__ import annotations
import tiktoken
import asyncio
from openai import OpenAI
from io import BytesIO
import os, re, inspect
from datetime import datetime
from urllib.parse import urlparse
import tempfile

# --- singleton lock for polling (prevents "Conflict: ... other getUpdates") ---
import os, sys, hashlib, psycopg2

_singleton_conn = None  # держим подключение открытым, пока живёт процесс

def _ensure_single_instance() -> None:
    """
    Берём advisory-lock в PostgreSQL. Если занято — выходим (вторая копия).
    Лок держится, пока открыт _singleton_conn (до завершения процесса).
    """
    global _singleton_conn
    if _singleton_conn is not None:
        return  # уже взяли

    dsn = getattr(settings, "database_url", None) or getattr(settings, "DATABASE_URL", None)
    if not dsn:
        # без БД лок не возьмём — просто предупреждаем
        log.warning("DATABASE_URL не задан — пропускаю singleton-lock (риск Conflict).")
        return

    # Стабильный ключ для advisory-lock
    lock_key = int(hashlib.sha1(f"{dsn}|{settings.telegram_bot_token}".encode("utf-8")).hexdigest()[:15], 16) % (2**31)
    try:
        _singleton_conn = psycopg2.connect(dsn)
        _singleton_conn.autocommit = True
        with _singleton_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            ok = cur.fetchone()[0]
        if not ok:
            log.error("‼️ Найдена другая копия бота (advisory-lock уже занят). Завершаюсь.")
            # мягкий выход, чтобы Railway перезапустил только один экземпляр
            sys.exit(0)
        log.info("✅ Singleton-lock получен (pg_advisory_lock).")
    except Exception:
        log.exception("Не удалось взять singleton-lock. Риск Conflict остаётся.")

# --- post_init для Application: удаляем webhook перед polling ---
async def _post_init(app: "Application"):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("✅ Webhook удалён, pending updates сброшены.")
    except Exception:
        log.exception("Не удалось удалить webhook")


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
_oa_client = OpenAI(api_key=settings.openai_api_key)

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

TELEGRAM_CHUNK = 3500  # безопасный размер сообщения

def _split_for_tg(text: str, limit: int = TELEGRAM_CHUNK):
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

# /dialog <id> — сделать диалог активным
async def dialog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    args = context.args or []

    # если id не передан — просто покажем список диалогов
    if not args:
        return await dialogs(update, context)

    # парсим id
    try:
        did = int(args[0])
    except Exception:
        return await m.reply_text("Использование: /dialog <id>")

    try:
        with SessionLocal() as db:
            # проверяем, что диалог принадлежит пользователю
            row = db.execute(sa_text("""
                SELECT d.id
                FROM dialogs d
                JOIN users u ON u.id = d.user_id
                WHERE u.tg_user_id = :tg AND d.id = :did
                LIMIT 1
            """), {"tg": update.effective_user.id, "did": did}).fetchone()

            if not row:
                return await m.reply_text("⚠ Диалог не найден или не принадлежит вам.")

            # если в проекте есть хелпер — используем его
            try:
                _set_active_dialog_id  # type: ignore
                _set_active_dialog_id(db, update.effective_user.id, did)  # noqa
            except NameError:
                # запасной путь: держим активный диалог в user_data
                context.user_data["active_dialog_id"] = did

            await m.reply_text(f"✅ Активный диалог: {did}")
            # опционально сразу показать /stats
            return await stats(update, context)

    except Exception:
        log.exception("dialog_cmd failed")
        return await m.reply_text("⚠ Не удалось переключить диалог. Попробуйте ещё раз.")


async def _send_long(m, text: str):
    """Отправляет текст пачками, если он длиннее лимита Telegram."""
    for chunk in _split_for_tg(text):
        await m.reply_text(chunk)

async def _chat_full(model: str, messages: list, temperature: float = 0.3, max_turns: int = 6):
    """
    Вызывает Chat Completions столько раз, сколько нужно, пока finish_reason != 'length'
    или не исчерпан лимит max_turns. Возвращает полный склеенный ответ.
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
            max_tokens=1024,  # можно увеличить, но мы всё равно автопродолжим
        )
        choice = resp.choices[0]
        piece = choice.message.content or ""
        full += piece
        finish = choice.finish_reason
        if finish != "length":
            break
        # просим продолжить
        hist.append({"role": "assistant", "content": piece})
        hist.append({"role": "user", "content": "Продолжай с того места. Не повторяйся."})
    return full

def _get_active_dialog_id(db, tg_id: int) -> int | None:
    row = db.execute(sa_text("""
        SELECT d.id
        FROM dialogs d
        JOIN users u ON u.id = d.user_id
        WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE
        ORDER BY COALESCE(d.last_message_at, to_timestamp(0)) DESC,
                 COALESCE(d.created_at,      to_timestamp(0)) DESC,
                 d.id DESC
        LIMIT 1
    """), {"tg": tg_id}).first()
    return row[0] if row else None

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
    """Голос → Whisper → как обычный текстовый запрос в активный диалог."""
    m = update.effective_message or update.message
    voice = m.voice or m.audio
    if not voice:
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")

    try:
        # 1) Скачиваем голос в .ogg (Telegram voice — OGG/Opus)
        import tempfile, pathlib
        file = await context.bot.get_file(voice.file_id)
        tmpdir = tempfile.mkdtemp(prefix="tg_voice_")
        ogg_path = str(pathlib.Path(tmpdir) / "voice.ogg")   # Важно: корректное расширение
        await file.download_to_drive(ogg_path)

        # 2) Отправляем в OpenAI (whisper-1 по умолчанию)
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        model_name = getattr(settings, "OPENAI_TRANSCRIBE_MODEL", "whisper-1")
        with open(ogg_path, "rb") as f:
            tr = client.audio.transcriptions.create(model=model_name, file=f)  # file name=voice.ogg → ок
        text = (getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else None) or "").strip()
        if not text:
            return await m.reply_text("⚠ Не удалось распознать речь. Скажите ещё раз, пожалуйста.")

        # 3) Сохраняем «user» в ТЕКУЩИЙ активный диалог
        with SessionLocal() as db:
            did = _get_active_dialog_id(db, update.effective_user.id) or _create_new_dialog_for_tg(db, update.effective_user.id)
            _save_message(db, did, "user", text)  # в твоём проекте уже есть хелпер сохранения

        # 4) Переиспользуем обычный on_text
        update.effective_message.text = text
        return await on_text(update, context)

    except Exception:
        log.exception("on_voice failed")
        return await m.reply_text("⚠ Не удалось обработать голосовое. Попробуйте ещё раз.")

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

# --- KB sync (унифицированный вызов indexer) ---
import os, re, inspect, asyncio

async def kb_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")
    await m.reply_text("🔄 Синхронизация запущена...")

    try:
        from bot.knowledge_base import indexer

        explicit = getattr(settings, "kb_sync_entrypoint", None) or os.getenv("KB_SYNC_ENTRYPOINT")
        fn = getattr(indexer, explicit, None) if explicit else None
        if not fn:
            for name in ("sync_kb","sync_all","sync_from_yandex","sync","run_sync","full_sync",
                         "reindex","index_all","ingest_all","ingest","main"):
                if hasattr(indexer, name) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name); break
        if not fn:
            for name in dir(indexer):
                if name.startswith("_"): continue
                if re.search(r"(sync|index|ingest)", name, re.I) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name); break
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
                kwargs[p.name] = None  # обязательный неизвестный параметр
        def _call():
            try: return fn(**kwargs)
            finally:
                if session_to_close is not None:
                    try: session_to_close.close()
                    except Exception: pass
        result = await asyncio.to_thread(_call)

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
        log.exception("kb_sync failed")
        return await m.reply_text(f"⚠ Ошибка синхронизации: {e}")


KB_PAGE_SIZE = 10



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
    m = update.effective_message or update.message
    try:
        tg = update.effective_user.id
        with SessionLocal() as db:
            did = _create_new_dialog_for_tg(db, tg)
        await m.reply_text(f"✅ Создан диалог #{did}")
    except Exception:
        log.exception("dialog_new failed")
        await m.reply_text("⚠ Ошибка создания диалога")

def _create_new_dialog_for_tg(db, tg_id: int) -> int:
    uid = _exec_scalar(db, "SELECT id FROM users WHERE tg_user_id=:tg ORDER BY id LIMIT 1", tg=tg_id)
    if not uid:
        uid = _ensure_user(db, tg_id)

    today = datetime.now().date().isoformat()
    cnt = _exec_scalar(db, """
        SELECT count(*) FROM dialogs d
        JOIN users u ON u.id = d.user_id
        WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE
    """, tg=tg_id) or 0
    title = f"{today} | диалог {cnt+1}"

    did = _exec_scalar(db, """
        INSERT INTO dialogs (user_id, title, style, model, is_deleted, created_at, last_message_at)
        VALUES (:u, :t, :s, :m, FALSE, now(), NULL) RETURNING id
    """, u=uid, t=title, s="pro", m=settings.openai_model)
    db.commit()
    return did

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
            did = _get_active_dialog_id(db, update.effective_user.id)
            row = db.execute(sa_text(
                "SELECT id, title, model, style, created_at, last_message_at "
                "FROM dialogs WHERE id=:d"
            ), {"d": did}).fetchone()

            # Сообщения именно этого диалога
            msgs = db.execute(sa_text(
                "SELECT count(*) FROM messages WHERE dialog_id=:d"
            ), {"d": did}).scalar() or 0

            # Подключённые документы
            docs = db.execute(sa_text(
                "SELECT d.path "
                "FROM kb_documents d "
                "JOIN dialog_kb_links l ON l.document_id=d.id "
                "WHERE l.dialog_id=:d ORDER BY d.path"
            ), {"d": did}).fetchall()
            doc_list = "\n".join(f"• {r[0]}" for r in docs) or "—"

            total_dialogs = db.execute(sa_text(
                "SELECT count(*) FROM dialogs WHERE user_id=("
                "  SELECT id FROM users WHERE tg_user_id=:tg LIMIT 1)"
            ), {"tg": update.effective_user.id}).scalar() or 0

        text = (
            f"whoami: tg={update.effective_user.id}, role={'admin' if _is_admin(update.effective_user.id) else 'allowed'}, lang={context.user_data.get('lang','ru')}\n\n"
            f"Диалог: {row.id} — {row.title}\n"
            f"Модель: {row.model} | Стиль: {row.style}\n"
            f"Создан: {row.created_at or '-'} | Изменён: {row.last_message_at or '-'}\n"
            f"Подключённые документы ({len(docs)}):\n{doc_list}\n\n"
            f"Всего твоих диалогов: {total_dialogs} | Сообщений в этом диалоге: {msgs}"
        )
        return await m.reply_text(text)
    except Exception:
        log.exception("/stats failed")
        return await m.reply_text("⚠ Ошибка /stats")

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

KB_PAGE_SIZE = 10



async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        with SessionLocal() as db:
            tg_id = update.effective_user.id
            uid = _ensure_user(db, tg_id)
            did = _ensure_dialog(db, uid)
            db.execute(sa_text("DELETE FROM messages WHERE dialog_id=:d"),   {"d": did})
            db.execute(sa_text("DELETE FROM dialog_kb_links WHERE dialog_id=:d"), {"d": did})
            db.execute(sa_text("DELETE FROM pdf_passwords WHERE dialog_id=:d"),   {"d": did})
            db.execute(sa_text("UPDATE dialogs SET last_message_at=NULL WHERE id=:d"), {"d": did})
            db.commit()
        context.user_data.clear()
        await m.reply_text("♻️ Диалог очищен: история, привязки БЗ и пароли PDF сброшены.")
    except Exception:
        log.exception("reset failed")
        await m.reply_text("⚠ Не удалось сбросить диалог.")

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

# фильтруем только чатовые семейства и исключаем всё лишнее
def _keep_chat_model(mid: str) -> bool:
    m = mid.lower()
    if any(x in m for x in ["embedding", "text-embedding", "dall-e", "whisper", "tts", "audio", "moderation", "computer-use"]):
        return False
    # скрываем явный мусор/несуществующие
    if m.startswith("babbage") or m.startswith("davinci") or m.startswith("curie") or m.startswith("ada"):
        return False
    if m.startswith("gpt-5"):  # не показываем
        return False
    # оставляем gpt-4/4o/4.1/o4, 3.5, chatgpt-4o-latest
    return any(x in m for x in ["gpt-4", "gpt-3.5", "chatgpt-4o", "o4"])

def _sort_models(models):
    # сортируем по created (desc), если есть; иначе по эвристике
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

        # сохраним для "Показать ещё"
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

async def model_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        data = q.data or ""
        if data in ("model:close", "model:nop"):
            try: await q.delete_message()
            except Exception: pass
            return

        if data.startswith("model:more:"):
            page = int(data.split(":")[-1])
            ids = context.user_data.get("all_models_sorted") or []
            return await q.edit_message_reply_markup(reply_markup=_page_models(ids, page))

        if data.startswith("model:set:"):
            mid = data.split(":", 2)[-1]
            # проверим быстро доступность
            try:
                client = OpenAI(api_key=settings.openai_api_key)
                client.chat.completions.create(model=mid, messages=[{"role": "user", "content": "ping"}], max_tokens=1)
            except Exception:
                return await q.edit_message_text(f"❌ Не удалось выбрать модель «{mid}». Попробуйте другую.")

            tg = update.effective_user.id
            with SessionLocal() as db:
                did = _exec_scalar(db, """
                    SELECT d.id
                    FROM dialogs d
                    JOIN users u ON u.id = d.user_id
                    WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE
                    ORDER BY d.created_at DESC
                    LIMIT 1
                """, tg=tg)
                if not did:
                    return await q.edit_message_text("Нет активного диалога. Нажми /dialog_new.")
                db.execute(sa_text("UPDATE dialogs SET model=:m WHERE id=:d"), {"m": mid, "d": did})
                db.commit()
            return await q.edit_message_text(f"✅ Установлена модель: {mid}")
    except Exception:
        log.exception("model_cb failed")
        try: await q.message.reply_text("⚠ Ошибка выбора модели")
        except Exception: pass


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

DIALOGS_PAGE_SIZE = 6

async def dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message or update.message
    try:
        tg = update.effective_user.id
        with SessionLocal() as db:
            ds = _exec_all(db, """
                SELECT d.id, COALESCE(d.title,'')
                FROM dialogs d
                JOIN users u ON u.id = d.user_id
                WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE
                ORDER BY d.created_at DESC
            """, tg=tg)

        if not ds:
            kb = [[InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")]]
            return await m.reply_text("Диалогов нет.", reply_markup=InlineKeyboardMarkup(kb))

        page = 1
        pages = max(1, (len(ds) + DIALOGS_PAGE_SIZE - 1) // DIALOGS_PAGE_SIZE)
        beg = (page - 1) * DIALOGS_PAGE_SIZE
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
        rows.append([InlineKeyboardButton("➕ Новый диалог", callback_data="dlg:new")])

        await m.reply_text("Мои диалоги:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        log.exception("dialogs failed")
        await m.reply_text("⚠ Ошибка /dialogs")

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
                ds = _exec_all(db, """
                    SELECT d.id, COALESCE(d.title,'')
                    FROM dialogs d
                    JOIN users u ON u.id = d.user_id
                    WHERE u.tg_user_id = :tg AND d.is_deleted = FALSE
                    ORDER BY d.created_at DESC
                """, tg=tg)
            total = len(ds)
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
                    InlineKeyboardButton("🗑️", callback_data=f"dlg:delete:{did}"),
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
            # активируем диалог, НЕ меняя created_at — используем last_message_at
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
                msgs = _exec_all(db, """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE dialog_id = :d
                    ORDER BY created_at
                """, d=dlg_id)
            lines = ["# Экспорт диалога", ""]
            for role, content, _ in msgs:
                who = "Пользователь" if role == "user" else "Бот"
                lines.append(f"**{who}:**\n{content}\n")
            data_bytes = "\n".join(lines).encode("utf-8")
            file = (BufferedInputFile if HAS_BUFFERED else InputFile)(data_bytes, filename=f"dialog_{dlg_id}.md")  # type: ignore
            return await q.message.reply_document(document=file, caption="Экспорт готов")

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
        
def build_app() -> Application:
    # alembic autoupgrade / ленивые миграции
    apply_migrations_if_needed()

    # гарантия единственного инстанса (до старта polling)
    _ensure_single_instance()

    # собираем Application с post_init-хуком, который удаляет webhook
    app = ApplicationBuilder() \
        .token(settings.telegram_bot_token) \
        .post_init(_post_init) \
        .build()

    app.add_error_handler(error_handler)

    # --- ваши хендлеры (оставляю как у вас, но см. пункт 2 про дубликаты) ---
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
    # админки по БЗ
    app.add_handler(CommandHandler("kb_chunks", kb_chunks_cmd))
    app.add_handler(CommandHandler("kb_reindex", kb_reindex))
    app.add_handler(CommandHandler("kb_sync", kb_sync_admin))
    # сообщения
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

async def kb_sync_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-синк БЗ: надёжно вызывает entrypoint из bot.knowledge_base.indexer, 
    даже если внутри он запускает свой asyncio.run()."""
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")
    await m.reply_text("🔄 Синхронизация запущена...")

    import asyncio, inspect, os, re
    try:
        from bot.knowledge_base import indexer

        explicit = getattr(settings, "kb_sync_entrypoint", None) or os.getenv("KB_SYNC_ENTRYPOINT")
        fn = getattr(indexer, explicit, None) if explicit else None
        if not fn:
            for name in ("sync_kb","sync_all","sync_from_yandex","sync","run_sync","full_sync",
                         "reindex","index_all","ingest_all","ingest","main"):
                if hasattr(indexer, name) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name); break
        if not fn:
            for name in dir(indexer):
                if name.startswith("_"): 
                    continue
                if re.search(r"(sync|index|ingest)", name, re.I) and callable(getattr(indexer, name)):
                    fn = getattr(indexer, name); break
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

        # Безопасный запуск: корутины — await, синхронные (в т.ч. с asyncio.run внутри) — в отдельном треде.
        import asyncio
        import inspect as _inspect
        try:
            if _inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, lambda: fn(**kwargs))
        finally:
            if session_to_close is not None:
                try: session_to_close.close()
                except Exception: pass

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
    if page > 1:   nav.append(InlineKeyboardButton("«", callback_data=f"kb:page:{page-1}"))
    nav.append(InlineKeyboardButton(f"Страница {page}/{pages}", callback_data="kb:nop"))
    if page < pages: nav.append(InlineKeyboardButton("»", callback_data=f"kb:page:{page+1}"))
    if nav: rows.append(nav)

    rows.append([
        InlineKeyboardButton("Все", callback_data="kb:mode:all"),
        InlineKeyboardButton("Подключённые", callback_data="kb:mode:linked"),
        InlineKeyboardButton("Доступные", callback_data="kb:mode:avail"),
    ])
    rows.append([InlineKeyboardButton("🗘 Синхронизация", callback_data="kb:sync")])
    rows.append([InlineKeyboardButton("📊 Статус БЗ", callback_data="kb:status")])

    text = "Меню БЗ: выберите документы для подключения к активному диалогу."
    return text, InlineKeyboardMarkup(rows), pages, page, linked

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
                    d=did, doc=doc_id)
                if exists:
                    db.execute(sa_text("DELETE FROM dialog_kb_links WHERE dialog_id=:d AND document_id=:doc"),
                               {"d": did, "doc": doc_id})
                else:
                    db.execute(sa_text(
                        "INSERT INTO dialog_kb_links (dialog_id, document_id, created_at) "
                        "VALUES (:d,:doc,now()) ON CONFLICT DO NOTHING"),
                        {"d": did, "doc": doc_id})
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

async def kb_chunks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /kb_chunks <size> <overlap> [<kb_top_k>] — меняет параметры в рантайме (до рестарта)."""
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")

    parts = (m.text or "").split()
    if len(parts) < 3:
        s, o, _ = _get_chunk_params()
        return await m.reply_text(
            "Использование: /kb_chunks <size> <overlap> [<kb_top_k>]\n"
            f"Текущие: size={s}, overlap={o}. Для постоянной смены правьте CHUNK_SIZE/CHUNK_OVERLAP в env."
        )

    try:
        size = int(parts[1]); overlap = int(parts[2])
        if size < 200 or size > 8000:
            return await m.reply_text("size должен быть 200..8000.")
        if overlap < 0 or overlap >= size:
            return await m.reply_text("overlap должен быть 0..(size-1).")

        for attr in ("chunk_size", "CHUNK_SIZE"):
            if hasattr(settings, attr):
                setattr(settings, attr, size)
        for attr in ("chunk_overlap", "CHUNK_OVERLAP"):
            if hasattr(settings, attr):
                setattr(settings, attr, overlap)

        if len(parts) >= 4:
            kb_top_k = int(parts[3])
            for attr in ("kb_top_k", "KB_TOP_K"):
                if hasattr(settings, attr):
                    setattr(settings, attr, kb_top_k)

        s, o, _ = _get_chunk_params()
        await m.reply_text(
            f"✅ Параметры обновлены: size={s}, overlap={o}. "
            f"Для пересчёта существующих файлов — /kb_reindex."
        )
    except Exception:
        log.exception("/kb_chunks failed")
        await m.reply_text("⚠ Не удалось применить параметры.")

def _get_chunk_params():
    """Возвращает (size, overlap, embedding_model) из settings (snake/UPPER case)."""
    size = getattr(settings, "chunk_size", None) or getattr(settings, "CHUNK_SIZE", None) or 1200
    overlap = getattr(settings, "chunk_overlap", None) or getattr(settings, "CHUNK_OVERLAP", None) or 200
    emb = getattr(settings, "openai_embedding_model", None) or getattr(settings, "OPENAI_EMBEDDING_MODEL", None) or "text-embedding-3-large"
    return int(size), int(overlap), str(emb)
    
async def kb_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полный реиндекс: чистим чанки, сбрасываем etag/chunk_schema и запускаем обычный синк. Только для админов."""
    m = update.effective_message or update.message
    if not _is_admin(update.effective_user.id):
        return await m.reply_text("⛔ Доступ только админам.")

    size, overlap, emb = _get_chunk_params()
    await m.reply_text(f"♻️ Полный реиндекс: size={size}, overlap={overlap}, embedding={emb}.\n"
                       f"Очищаю чанки и перезапускаю синхронизацию…")
    try:
        with SessionLocal() as db:
            db.execute(sa_text("DELETE FROM kb_chunks"))
            db.execute(sa_text("UPDATE kb_documents SET etag=NULL, updated_at=now()"))
            try:
                db.execute(sa_text("UPDATE kb_documents SET chunk_schema=NULL"))
            except Exception:
                pass
            db.commit()
        return await kb_sync_admin(update, context)
    except Exception as e:
        log.exception("kb_reindex failed")
        return await m.reply_text(f"⚠ Ошибка реиндекса: {e}")

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

            chunks = retriever.search(did, query_embedding, top_k=max(settings.KB_TOP_K, 6))
            chunks = diversify_chunks(chunks, k=settings.KB_TOP_K)
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

def diversify_chunks(chunks, k):
    """Берёт больше кандидатов и жадно набирает до k, стараясь не брать
    много фрагментов из одного и того же документа подряд."""
    if not chunks:
        return []
    # ожидаем, что в чанке есть .document_id или meta с id/path
    def doc_id_of(ch):
        if hasattr(ch, "document_id"):
            return ch.document_id
        if isinstance(ch, dict):
            return ch.get("document_id") or (ch.get("meta") or {}).get("document_id")
        return None

    pool = chunks[: max(k*3, k)]  # больше кандидатов
    picked, seen = [], set()
    # 1 проход — по одному фрагменту с каждого doc
    for ch in pool:
        did = doc_id_of(ch)
        if did is not None and did not in seen:
            picked.append(ch); seen.add(did)
            if len(picked) >= k:
                return picked
    # добираем оставшиеся — по релевантности
    for ch in pool:
        if ch in picked:
            continue
        picked.append(ch)
        if len(picked) >= k:
            break
    return picked

