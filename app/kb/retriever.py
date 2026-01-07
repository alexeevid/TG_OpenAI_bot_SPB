# app/kb/retriever.py
from __future__ import annotations

from typing import Optional, List

from ..db.repo_kb import KBRepo
from ..clients.openai_client import OpenAIClient
from ..core.types import RetrievedChunk


class Retriever:
    def __init__(self, kb_repo: KBRepo, openai: OpenAIClient, dim: int):
        self._repo = kb_repo
        self._openai = openai
        self._dim = dim  # dim сейчас не используется, но оставляем для совместимости

    def retrieve(
        self,
        query: str,
        dialog_id: int,
        top_k: int = 6,
        allowed_document_ids: Optional[List[int]] = None,
    ) -> list[RetrievedChunk]:
        # Backward compatible: some code expects is_enabled(); some not.
        if not self._openai:
            return []
        if hasattr(self._openai, "is_enabled") and callable(getattr(self._openai, "is_enabled")):
            if not self._openai.is_enabled():
                return []

        query = (query or "").strip()
        if not query:
            return []

        # 1) embed query
        emb = self._openai.embeddings([query], model="text-embedding-3-large")[0]

        # 2) vector search in DB
        # repo_kb.search_by_embedding returns list[dict] with keys:
        # chunk_id, document_id, chunk_order, text, score
        rows = self._repo.search_by_embedding(
            emb,
            limit=int(top_k),
            document_ids=allowed_document_ids,
        )

        if not rows:
            return []

        # 3) enrich with doc brief (title/path)
        out: List[RetrievedChunk] = []
        doc_cache: dict[int, dict] = {}

        for r in rows:
            chunk_id = int(r.get("chunk_id"))
            doc_id = int(r.get("document_id"))
            text = str(r.get("text") or "")
            score = float(r.get("score") or 0.0)

            if doc_id not in doc_cache:
                doc_cache[doc_id] = self._repo.get_document_brief(doc_id) or {"title": None, "path": None}

            brief = doc_cache[doc_id]

            out.append(
                RetrievedChunk(
                    id=chunk_id,
                    text=text,
                    score=score,
                    document_id=doc_id,
                    document_title=brief.get("title"),
                    document_path=brief.get("path"),
                )
            )

        return out
