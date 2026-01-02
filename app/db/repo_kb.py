from __future__ import annotations

from typing import List, Tuple, Sequence

from sqlalchemy import text as sqltext
from sqlalchemy.orm import Session

from .models import KBDocument, KBChunk
try:
    from pgvector.sqlalchemy import Vector
except Exception:
    Vector = None


class KBRepo:
    def __init__(self, session_factory, dim: int):
        self.sf = session_factory
        self.dim = int(dim)

    def upsert_document(self, *, resource_id: str, path: str, title: str | None = None) -> int:
        with self.sf() as s:  # type: Session
            doc = s.query(KBDocument).filter_by(resource_id=resource_id).first()
            if not doc:
                doc = KBDocument(resource_id=resource_id, path=path, title=title or path)
                s.add(doc)
                s.commit()
                s.refresh(doc)
                return int(doc.id)

            changed = False
            if doc.path != path:
                doc.path = path
                changed = True
            if title and doc.title != title:
                doc.title = title
                changed = True
            if changed:
                s.add(doc)
                s.commit()
            return int(doc.id)

    def delete_chunks_by_document_id(self, document_id: int) -> None:
        with self.sf() as s:
            s.query(KBChunk).filter_by(document_id=document_id).delete(synchronize_session=False)
            s.commit()

    def delete_chunks_by_resource_id(self, resource_id: str) -> None:
        with self.sf() as s:
            doc = s.query(KBDocument).filter_by(resource_id=resource_id).first()
            if not doc:
                return
            s.query(KBChunk).filter_by(document_id=doc.id).delete(synchronize_session=False)
            s.commit()

    def insert_chunks_bulk(self, rows: Sequence[tuple[int, int, str, list[float]]]) -> None:
        with self.sf() as s:
            objs = [
                KBChunk(document_id=doc_id, chunk_order=order, text=txt, embedding=emb)
                for (doc_id, order, txt, emb) in rows
            ]
            s.add_all(objs)
            s.commit()

    def search_by_embedding(self, query_emb: list[float], top_k: int) -> List[Tuple[int, str, float]]:
        top_k = int(top_k)
        with self.sf() as s:
            if Vector is None:
                return []

            stmt = sqltext(
                """
                SELECT id, text, (embedding <=> :q) AS distance
                FROM kb_chunks
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> :q
                LIMIT :k
                """
            )
            rows = s.execute(stmt, {"q": query_emb, "k": top_k}).fetchall()

            out: List[Tuple[int, str, float]] = []
            for rid, txt, dist in rows:
                d = float(dist) if dist is not None else 1.0
                out.append((int(rid), str(txt), 1.0 - d))
            return out
