from __future__ import annotations
import logging
from typing import Dict, Any, List
from sqlalchemy import select
from sqlalchemy.orm import Session
from bot.yandex_client import list_files, download_to_bytes
from bot.db.models import KbDocument, KbChunk
from bot.settings import load_settings
from bot.openai_helper import embed
from bot.utils.parsers import extract_text_from_bytes
from bot.utils.text import chunk_text

log = logging.getLogger(__name__)
_settings = load_settings()

def sync_kb(session: Session) -> Dict[str, Any]:
    """Синхронизация документов из Я.Диска. Upsert документов и чанков с эмбеддингами."""
    files = list_files(_settings.yandex_root_path)
    updated = 0
    skipped = 0
    for f in files:
        path = f["path"]
        etag = f.get("etag")
        mime = f.get("mime", "")
        size = int(f.get("size") or 0)
        doc = session.execute(select(KbDocument).where(KbDocument.path == path)).scalar_one_or_none()
        if doc and doc.etag == etag and doc.is_active:
            skipped += 1
            continue
        # download & parse
        raw = download_to_bytes(path)
        text, meta = extract_text_from_bytes(raw, mime)
        chunks = chunk_text(text, _settings.chunk_size, _settings.chunk_overlap)
        if not chunks:
            continue
        # embeddings (sync bridge)
        embs = _embed_sync(chunks)
        # upsert doc
        if doc is None:
            doc = KbDocument(path=path, etag=etag, mime=mime, bytes=size, is_active=True)
            session.add(doc)
            session.flush()
        else:
            doc.etag = etag
            doc.mime = mime
            doc.bytes = size
            doc.is_active = True
            session.flush()
            session.query(KbChunk).filter(KbChunk.document_id == doc.id).delete()
        # insert chunks
        for i, content in enumerate(chunks):
            kc = KbChunk(document_id=doc.id, chunk_index=i, content=content, meta=None, embedding=embs[i])
            session.add(kc)
        session.commit()
        updated += 1
    return {"updated": updated, "skipped": skipped, "total": len(files)}

def _embed_sync(chunks: List[str]) -> List[List[float]]:
    import asyncio
    async def _do():
        return await embed([c for c in chunks])
    return asyncio.get_event_loop().run_until_complete(_do())
