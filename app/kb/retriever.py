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
        self._dim = dim

    def retrieve(
        self,
        query: str,
        dialog_id: int,
        top_k: int = 6,
        allowed_document_ids: Optional[List[int]] = None,
    ) -> list[RetrievedChunk]:
        if not self._openai or not self._openai.is_enabled():
            return []

        emb = self._openai.embeddings([query], model="text-embedding-3-large")[0]
        rows = self._repo.search_by_embedding(emb, top_k, allowed_document_ids=allowed_document_ids)

        out: List[RetrievedChunk] = []
        # Подтянем doc brief одним проходом (кеш локально)
        doc_cache = {}

        for (chunk_id, text, dist, doc_id) in rows:
            if doc_id not in doc_cache:
                doc_cache[doc_id] = self._repo.get_document_brief(doc_id) or {"title": None, "path": None}
            brief = doc_cache[doc_id]
            out.append(
                RetrievedChunk(
                    id=int(chunk_id),
                    text=str(text),
                    score=float(dist),
                    document_id=int(doc_id),
                    document_title=brief.get("title"),
                    document_path=brief.get("path"),
                )
            )
        return out
