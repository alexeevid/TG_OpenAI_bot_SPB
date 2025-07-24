from sqlalchemy import text
from bot.db.session import SessionLocal

class Retriever:
    def __init__(self, session_factory=SessionLocal, top_k: int = 5):
        self.sf = session_factory
        self.top_k = top_k

    async def search(self, query: str, top_k: int | None = None, document_ids: list[int] | None = None):
        k = top_k or self.top_k
        sql = "SELECT dc.document_id, dc.text FROM document_chunks dc"
        params = {}
        if document_ids:
            sql += " WHERE dc.document_id = ANY(:doc_ids)"
            params["doc_ids"] = document_ids
        sql += " LIMIT :k"
        params["k"] = k
        with self.sf() as s:
            rows = s.execute(text(sql), params).all()
            return [(r.document_id, r.text, 0.0) for r in rows]
