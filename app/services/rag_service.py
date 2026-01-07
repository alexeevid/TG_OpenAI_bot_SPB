# app/services/rag_service.py
from __future__ import annotations

from ..kb.retriever import Retriever
from ..core.types import RetrievedChunk
from ..services.dialog_kb_service import DialogKBService


class RagService:
    def __init__(self, retriever: Retriever, dialog_kb: DialogKBService):
        self._r = retriever
        self._dkb = dialog_kb

    def retrieve(
        self,
        query: str,
        dialog_id: int,
        top_k: int = 6,
        *,
        min_score: float = 0.35,
    ) -> list[RetrievedChunk]:
        """Возвращает релевантные чанки для RAG."""
        if not self._dkb.rag_enabled(dialog_id):
            return []

        allowed = self._dkb.allowed_document_ids(dialog_id)
        if not allowed:
            return []

        results = self._r.retrieve(
            query,
            dialog_id=dialog_id,
            top_k=top_k,
            allowed_document_ids=allowed,
        )

        if not results:
            return []

        try:
            ms = float(min_score)
        except Exception:
            ms = 0.0

        filtered = [r for r in results if (r.score is not None and float(r.score) >= ms)]
        return filtered
