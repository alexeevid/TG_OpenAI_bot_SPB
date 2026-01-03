from __future__ import annotations

from typing import List, Optional

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
        allowed_document_ids: Optional[list[int]] = None,
    ) -> list[RetrievedChunk]:
        if not self._openai or not self._openai.is_enabled():
            return []

        # embedding модели оставляем как у вас ранее (и в openai_client)
        emb = self._openai.embeddings([query], model="text-embedding-3-large")[0]

        rows = self._repo.search_by_embedding(emb, top_k, allowed_document_ids=allowed_document_ids)
        out: List[RetrievedChunk] = []
        for (chunk_id, text, dist, doc_id) in rows:
            out.append(RetrievedChunk(id=chunk_id, text=text, score=dist))
        return out
