# bot/knowledge_base/indexer.py
from __future__ import annotations

from typing import Tuple, Optional, Dict, Any, List

from sqlalchemy import select, text as sql_text, func
from sqlalchemy.orm import Session

from bot.settings import load_settings
from bot.yandex_client import list_files, download_to_bytes
from bot.openai_helper import embed
from bot.utils.parsers import extract_text_from_bytes
from bot.utils.text import chunk_text
from bot.db.models import KbDocument, KbChunk

_settings = load_settings()


def _extract_text_and_pages(blob: bytes, mime: str) -> Tuple[str, Optional[int]]:
    """
    Унифицируем разные сигнатуры extract_text_from_bytes:
    - может вернуть строку
    - может вернуть (строка, meta: dict|int|None)
    """
    try:
        rv = extract_text_from_bytes(blob, mime=mime)
    except TypeError:
        # если старая сигнатура без mime
        rv = extract_text_from_bytes(blob)

    pages = None
    text = ""

    if isinstance(rv, tuple):
        text = rv[0] if rv and isinstance(rv[0], str) else ""
        meta = rv[1] if len(rv) > 1 else None
        if isinstance(meta, dict):
            pages = meta.get("pages") or meta.get("num_pages")
        elif isinstance(meta, int):
            pages = meta
    elif isinstance(rv, str):
        text = rv

    return text or "", pages


def _get_chunk_params() -> Tuple[int, int, str]:
    size = getattr(_settings, "chunk_size", None) or getattr(_settings, "CHUNK_SIZE", None) or 1200
    overlap = getattr(_settings, "chunk_overlap", None) or getattr(_settings, "CHUNK_OVERLAP", None) or 200
    emb = getattr(_settings, "openai_embedding_model", None) or getattr(_settings, "OPENAI_EMBEDDING_MODEL", None) or "text-embedding-3-large"
    return int(size), int(overlap), str(emb)


def _upsert_document(session: Session, *, path: str, etag: str, mime: str, pages: Optional[int], size_bytes: int) -> int:
    """
    Делает upsert по path и гарантированно проставляет updated_at = NOW().
    Возвращает id документа.
    Предпочтён SQL — так мы не напоримся на NULL в updated_at от ORM.
    """
    sql = sql_text("""
        INSERT INTO kb_documents (path, etag, mime, pages, bytes, updated_at, is_active)
        VALUES (:path, :etag, :mime, :pages, :bytes, NOW(), TRUE)
        ON CONFLICT (path) DO UPDATE
        SET etag = EXCLUDED.etag,
            mime = EXCLUDED.mime,
            pages = EXCLUDED.pages,
            bytes = EXCLUDED.bytes,
            updated_at = NOW(),
            is_active = TRUE
        RETURNING id
    """)
    doc_id = session.execute(sql, {
        "path": path,
        "etag": etag,
        "mime": mime,
        "pages": pages,
        "bytes": int(size_bytes),
    }).scalar_one()
    return int(doc_id)


def sync_kb(session: Session) -> Dict[str, int]:
    """
    Полная синхронизация каталога Y.Disk -> kb_documents/kb_chunks.
    - Скачиваем файл только если поменялся etag/размер.
    - Для новых/обновлённых: нарезаем чанки, считаем эмбеддинги, пересобираем kb_chunks.
    - updated_at проставляется на уровне SQL (NOW()).
    """
    root = _settings.yandex_root_path
    files = list_files(root)

    updated = 0
    skipped = 0

    # Параметры чанкования
    chunk_size, chunk_overlap, _emb_model = _get_chunk_params()

    for f in files:
        path: str = f["path"]
        etag: str = f.get("etag") or ""
        mime: str = f.get("mime") or ""
        size_bytes: int = int(f.get("size") or 0)

        # Быстрая проверка: есть ли запись и совпадает ли etag/bytes
        existing: Optional[KbDocument] = session.execute(
            select(KbDocument).where(KbDocument.path == path)
        ).scalar_one_or_none()

        if existing and existing.etag == etag and int(existing.bytes or 0) == size_bytes and existing.is_active:
            skipped += 1
            continue

        # 1) скачиваем файл
        blob = download_to_bytes(path)

        # 2) извлекаем текст и страницы
        text, pages = _extract_text_and_pages(blob, mime)

        # 3) чанкование
        chunks = chunk_text(text, size=chunk_size, overlap=chunk_overlap)

        if not chunks:
            # Защита от пустых текстов — документ помечаем активным, но чанков нет
            doc_id = _upsert_document(session, path=path, etag=etag, mime=mime, pages=pages, size_bytes=size_bytes)
            session.execute(sql_text("DELETE FROM kb_chunks WHERE document_id = :id"), {"id": doc_id})
            session.commit()
            updated += 1
            continue

        # 4) эмбеддинги (списком, чтобы не делать N запросов)
        embeddings = embed(chunks)  # синхронная версия в твоём helper-е

        # 5) upsert документа + пересборка чанков
        doc_id = _upsert_document(session, path=path, etag=etag, mime=mime, pages=pages, size_bytes=size_bytes)
        session.execute(sql_text("DELETE FROM kb_chunks WHERE document_id = :id"), {"id": doc_id})

        for i, content in enumerate(chunks):
            session.add(KbChunk(document_id=doc_id, chunk_index=i, content=content, meta=None, embedding=embeddings[i]))

        session.commit()
        updated += 1

    return {"updated": updated, "skipped": skipped, "total": len(files)}


# --- совместимость со старым API бота ---
def sync_all(SessionLocal, settings=None) -> tuple[int, int]:
    """
    Обёртка для telegram_bot.kb_sync — возвращает (updated_docs, total_chunks).
    """
    from sqlalchemy import select as _select
    with SessionLocal() as s:
        info = sync_kb(s)
        total_chunks = int(s.execute(_select(func.count()).select_from(KbChunk)).scalar() or 0)
        return int(info.get("updated", 0)), total_chunks


def sync_from_yandex(SessionLocal, settings=None) -> tuple[int, int]:
    """Алиас для совместимости с командами бота."""
    return sync_all(SessionLocal, settings)
