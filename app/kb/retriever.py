
from ..db.repo_kb import KBRepo
from ..clients.openai_client import OpenAIClient
from ..core.types import RetrievedChunk

class Retriever:
    def __init__(self, kb_repo: KBRepo, openai: OpenAIClient, dim: int):
        self._repo = kb_repo
        self._openai = openai
        self._dim = dim

    def retrieve(self, query: str, dialog_id: int, top_k: int = 6):
        if not self._openai or not self._openai.is_enabled():
            return []
        emb = self._openai.embeddings([query], model="text-embedding-3-large")[0]
        rows = self._repo.search_by_embedding(emb, top_k)
        return [RetrievedChunk(id=i, text=t, score=s) for (i,t,s) in rows]
