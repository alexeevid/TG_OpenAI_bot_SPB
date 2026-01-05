from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from app.db.repo_kb import KBRepo


@dataclass(frozen=True)
class Chunk:
    order: int
    text: str


def split_text(text: str, chunk_size: int, overlap: int) -> List[Chunk]:
    text = (text or "").strip()
    if not text:
        return []

    chunk_size = max(200, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size - 1))

    chunks: List[Chunk] = []
    n = len(text)
    start = 0
    order = 0

    while start < n:
        end = min(start + chunk_size, n)
        piece = text[start:end].strip()
        if piece:
            chunks.append(Chunk(order=order, text=piece))
            order += 1
        if end >= n:
            break
        start = max(0, end - overlap)

    return chunks


class KbIndexer:
    """
    Устойчивый индексатор БЗ.

    Поддерживает разные версии Embedder:
    - embed(texts)
    - embed_texts(texts)
    - embed_documents(texts)
    """

    def __init__(self, kb_repo: KBRepo, embedder, chunk_size: int, overlap: int):
        self._repo = kb_repo
        self._embedder = embedder
        self._chunk_size = int(chunk_size)
        self._overlap = int(overlap)

    def _embed(self, texts: List[str]) -> List[list[float]]:
        if hasattr(self._embedder, "embed"):
            return self._embedder.embed(texts)
        if hasattr(self._embedder, "embed_texts"):
            return self._embedder.embed_texts(texts)
        if hasattr(self._embedder, "embed_documents"):
            return self._embedder.embed_documents(texts)
        raise AttributeError(
            "Embedder has no supported method: expected one of embed / embed_texts / embed_documents"
        )

    def reindex_document(
        self,
        document_id: int | None = None,
        text: str | None = None,
        *,
        doc_id: int | None = None,
        document_text: str | None = None,
    ) -> int:
        did = int(document_id if document_id is not None else (doc_id or 0))
        if did <= 0:
            raise ValueError("reindex_document: document_id/doc_id is required")

        txt = text if text is not None else (document_text or "")
        txt = (txt or "").strip()

        chunks = split_text(txt, self._chunk_size, self._overlap)
        if not chunks:
            self._repo.delete_chunks_by_document_id(did)
            return 0

        embeddings = self._embed(chunks=[c.text for c in chunks])  # type: ignore

        rows: List[Tuple[int, int, str, list[float]]] = []
        for c, emb in zip(chunks, embeddings):
            rows.append((did, int(c.order), c.text, emb))

        self._repo.delete_chunks_by_document_id(did)
        self._repo.insert_chunks_bulk(rows)
        return len(rows)
