
# bot/knowledge_base/indexer.py
# Синхронизация БЗ: полностью синхронная (без asyncio.run внутри PTB event loop)
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openai import OpenAI
from sqlalchemy import text as sa_text
from datetime import datetime

from bot.yandex_client import list_files, download_to_bytes
from bot.db.models import KbDocument, KbChunk
from bot.settings import load_settings
from bot.utils.parsers import extract_text_from_bytes
from bot.utils.text import chunk_text

_settings = load_settings()

def _embed_sync(chunks: list[str]) -> list[list[float]]:
    if not chunks:
        return []
    client = OpenAI(api_key=_settings.openai_api_key)
    resp = client.embeddings.create(model=_settings.embedding_model, input=chunks)
    return [d.embedding for d in resp.data]

def sync_kb(session: Session):
    files = list_files(_settings.yandex_root_path)
    updated = 0; skipped = 0
    for f in files:
        path=f['path']; etag=f.get('etag'); mime=f.get('mime',''); size=int(f.get('size') or 0)
        doc=session.execute(select(KbDocument).where(KbDocument.path==path)).scalar_one_or_none()
        if doc and doc.etag==etag and doc.is_active:
            skipped+=1; continue
        raw=download_to_bytes(path)
        text, meta = extract_text_from_bytes(raw, mime)
        chunks = chunk_text(text, _settings.chunk_size, _settings.chunk_overlap)
        if not chunks:
            continue
        embs = _embed_sync(chunks)
        if doc is None:
            doc=KbDocument(path=path, etag=etag, mime=mime, bytes=size, is_active=True); session.add(doc); session.flush()
        else:
            doc.etag=etag; doc.mime=mime; doc.bytes=size; doc.is_active=True; session.flush(); session.query(KbChunk).filter(KbChunk.document_id==doc.id).delete()
        for i, content in enumerate(chunks):
            session.add(KbChunk(document_id=doc.id, chunk_index=i, content=content, meta=None, embedding=embs[i]))
        session.commit(); updated+=1
    return {'updated':updated,'skipped':skipped,'total':len(files)}

# --- совместимость со старым API бота ---
def sync_all(SessionLocal, settings=None):
    with SessionLocal() as s:
        info = sync_kb(s)
        updated = int(info.get("updated", 0))
        total_chunks = int(s.execute(select(func.count()).select_from(KbChunk)).scalar() or 0)
        return updated, total_chunks

def sync_from_yandex(SessionLocal, settings=None):
    return sync_all(SessionLocal, settings)

def upsert_kb_document(session, *, path: str, mime: str, bytes_: int, etag: str, pages: int | None):
    """
    Надёжный upsert в kb_documents. Никогда не передаёт updated_at=None.
    """
    sql = sa_text("""
        INSERT INTO kb_documents (path, etag, mime, pages, bytes, updated_at, is_active)
        VALUES (:p, :e, :m, :pg, :b, now(), TRUE)
        ON CONFLICT (path) DO UPDATE
        SET etag = EXCLUDED.etag,
            mime = EXCLUDED.mime,
            pages = EXCLUDED.pages,
            bytes = EXCLUDED.bytes,
            updated_at = now(),
            is_active = TRUE
        RETURNING id
    """)
    return session.execute(sql, {"p": path, "e": etag, "m": mime, "pg": pages, "b": bytes_}).scalar()
