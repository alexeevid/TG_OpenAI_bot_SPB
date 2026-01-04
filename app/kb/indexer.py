from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from app.db.repo_kb import KBRepo
from app.kb.embedder import Embedder


@dataclass(frozen=True)
class Chunk:
    order: int
    text: str


def split_text(text: str, chunk_size: int, overlap: int) -> List[Chunk]:
    text = (text or "").strip()
    if not text:
        return []

    if chunk_size <= 0:
        return [Chunk(order=0, text=text)]

    chunks: List[Chunk] = []
    start = 0
    order = 0
    n = len(text)

    while start < n:
        end = min(n, start + chunk_size)
        part = text[start:end].strip()
        if part:
            chunks.append(Chunk(order=order, text=part))
            order += 1
        if end >= n:
            break
        start = max(0, end - max(0, overlap))

    return chunks


class KbIndexer:
    """Indexes document text into pgvector-backed chunks.

    Best-practice properties:
    - deterministic chunking (chunk_order)
    - idempotent re-index: delete existing chunks by document_id then insert new ones
    - embeddings stored as VECTOR(dim) (pgvector) for fast similarity search in SQL
    """

    def __init__(self, kb_repo: KBRepo, embedder: Embedder, chunk_size: int, overlap: int):
        self._repo = kb_repo
        self._embedder = embedder
        self._chunk_size = int(chunk_size)
        self._overlap = int(overlap)

    def reindex_document(self, document_id: int, text: str) -> int:
        chunks = split_text(text, self._chunk_size, self._overlap)
        if not chunks:
            self._repo.delete_chunks_by_document_id(int(document_id))
            return 0

        embeddings = self._embedder.embed([c.text for c in chunks])
        rows: List[Tuple[int, int, str, list[float]]] = []
        for c, emb in zip(chunks, embeddings):
            rows.append((int(document_id), int(c.order), c.text, emb))

        self._repo.delete_chunks_by_document_id(int(document_id))
        self._repo.insert_chunks_bulk(rows)
        return len(rows)
