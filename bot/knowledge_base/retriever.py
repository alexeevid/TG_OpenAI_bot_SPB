
import numpy as np
from sqlalchemy import select
from bot.db.session import SessionLocal
from bot.db.models import DocumentChunk

class Retriever:
    def __init__(self, top_k: int = 5):
        self.top_k = top_k

    def search(self, query_embedding, restrict_doc_ids=None):
        q = np.array(query_embedding, dtype=np.float32).reshape(-1)
        with SessionLocal() as s:
            stmt = select(DocumentChunk)
            if restrict_doc_ids:
                stmt = stmt.where(DocumentChunk.document_id.in_(restrict_doc_ids))
            chunks = s.execute(stmt).scalars().all()

        q_norm = np.linalg.norm(q) + 1e-8
        scored = []
        for ch in chunks:
            v = np.array(ch.embedding, dtype=np.float32).reshape(-1)
            v_norm = np.linalg.norm(v) + 1e-8
            score = float(np.dot(q, v) / (q_norm * v_norm))
            scored.append((score, ch))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:self.top_k]]
