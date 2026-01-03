from __future__ import annotations

from ..kb.retriever import Retriever
from ..services.dialog_kb_service import DialogKBService
from ..core.types import RetrievedChunk


class RagService:
    def __init__(self, retriever: Retriever, dialog_kb: DialogKBService):
        self._r = retriever
        self._dkb = dialog_kb

    def retrieve(self, query: str, dialog_id: int, top_k: int = 6) -> list[RetrievedChunk]:
        if not self._dkb.is_rag_enabled(dialog_id):
            return []
        allowed = self._dkb.allowed_document_ids(dialog_id)
        if not allowed:
            return []
        return self._r.retrieve(query, dialog_id=dialog_id, top_k=top_k, allowed_document_ids=allowed)
