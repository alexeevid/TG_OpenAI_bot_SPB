from __future__ import annotations

from ..db.repo_kb import KBRepo
from ..kb.embedder import Embedder
from ..core.types import RetrievedChunk


class Retriever:
    def __init__(self, kb_repo: KBRepo, embedder: Embedder, *, top_k_default: int = 6):
        self._repo = kb_repo
        self._embedder = embedder
        self._top_k_default = int(top_k_default)

    def retrieve(self, query: str, dialog_id: int, top_k: int | None = None):
        q = (query or "").strip()
        if not q:
            return []
        k = int(top_k or self._top_k_default)
        emb = self._embedder.embed([q])[0]
        rows = self._repo.search_by_embedding(emb, k)
        return [RetrievedChunk(id=i, text=t, score=s) for (i, t, s) in rows]
