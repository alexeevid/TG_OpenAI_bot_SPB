from __future__ import annotations

from ..kb.retriever import Retriever
from ..core.types import RetrievedChunk
from .dialog_kb_service import DialogKBService


class RagService:
    def __init__(self, retriever: Retriever, dialog_kb: DialogKBService):
        self._r = retriever
        self._dkb = dialog_kb

    def retrieve(self, query: str, dialog_id: int, top_k: int = 6) -> list[RetrievedChunk]:
        allowed = self._dkb.allowed_document_ids(dialog_id)
        if not allowed:
            return []
        return self._r.retrieve(query, dialog_id, top_k, allowed_document_ids=allowed)
