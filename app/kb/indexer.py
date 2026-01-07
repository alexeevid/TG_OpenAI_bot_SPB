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

    Ключевая особенность:
    - embeddings вызываются БАТЧАМИ
    - защита от max_tokens_per_request
    """

    # ---- safety limits ----
    MAX_ITEMS_PER_BATCH = 32
    MAX_CHARS_PER_BATCH = 80_000  # грубый, но безопасный суррогат токенов

    def __init__(self, kb_repo: KBRepo, embedder, chunk_size: int, overlap: int):
        self._repo = kb_repo
        self._embedder = embedder
        self._chunk_size = int(chunk_size)
        self._overlap = int(overlap)

    # ---------- embeddings helpers ----------

    def _embed_raw(self, texts: List[str]) -> List[list[float]]:
        if hasattr(self._embedder, "embed"):
            return self._embedder.embed(texts)
        if hasattr(self._embedder, "embed_texts"):
            return self._embedder.embed_texts(texts)
        if hasattr(self._embedder, "embed_documents"):
            return self._embedder.embed_documents(texts)
        raise AttributeError(
            "Embedder has no supported method: expected one of embed / embed_texts / embed_documents"
        )

    def _embed_batched(self, texts: List[str]) -> List[list[float]]:
        """
        Делит embeddings на безопасные батчи.
        При ошибке — рекурсивно делит батч пополам.
        """
        if not texts:
            return []

        results: List[list[float]] = []

        batch: List[str] = []
        batch_chars = 0

        def flush_batch(b: List[str]) -> List[list[float]]:
            if not b:
                return []
            try:
                return self._embed_raw(b)
            except Exception:
                # fallback: делим пополам
                if len(b) == 1:
                    raise
                mid = len(b) // 2
                return flush_batch(b[:mid]) + flush_batch(b[mid:])

        for t in texts:
            t = t or ""
            t_len = len(t)

            if (
                batch
                and (
                    len(batch) >= self.MAX_ITEMS_PER_BATCH
                    or (batch_chars + t_len) >= self.MAX_CHARS_PER_BATCH
                )
            ):
                results.extend(flush_batch(batch))
                batch = []
                batch_chars = 0

            batch.append(t)
            batch_chars += t_len

        if batch:
            results.extend(flush_batch(batch))

        return results

    # ---------- public API ----------

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

        embeddings = self._embed_batched([c.text for c in chunks])

        rows: List[Tuple[int, int, str, list[float]]] = []
        for c, emb in zip(chunks, embeddings):
            rows.append((did, int(c.order), c.text, emb))

        self._repo.delete_chunks_by_document_id(did)
        self._repo.insert_chunks_bulk(rows)
        return len(rows)
