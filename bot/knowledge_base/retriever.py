from sqlalchemy import text
from bot.db.session import SessionLocal
class Retriever:
    def __init__(self, session_factory=SessionLocal, top_k: int = 5):
        self.sf = session_factory; self.top_k = top_k
    async def search(self, query: str, top_k: int | None = None, document_ids: list[int] | None = None):
        k = top_k or self.top_k
        sql = "SELECT dc.document_id, dc.text, 0.0 AS dist FROM document_chunks dc {where} LIMIT :k"
        where = ""
        params = {"k": k}
        if document_ids:
            where = "WHERE dc.document_id = ANY(:doc_ids)"
            params["doc_ids"] = document_ids
        sql = sql.format(where=where)
        with self.sf() as s:
            rows = s.execute(text(sql), params).all()
            return [(r.document_id, r.text, r.dist) for r in rows]
