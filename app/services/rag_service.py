
from ..kb.retriever import Retriever
from ..core.types import RetrievedChunk

class RagService:
    def __init__(self, retriever: Retriever):
        self._r = retriever
    def retrieve(self, query: str, dialog_id: int, top_k: int = 6) -> list[RetrievedChunk]:
        return self._r.retrieve(query, dialog_id, top_k)
