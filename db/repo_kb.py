from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from json import dumps, loads

from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext


class KBRepo:
    def __init__(self, sf, dim: int):
        self.sf = sf
        self.dim = dim

    # ---------- documents ----------
    def catalog(
        self,
        *,
        page: int = 1,
        page_size: int = 10,
        search: str = "",
    ) -> Tuple[List[Dict[str, Any]], int]:
        page = max(1, int(page))
        page_size = min(20, max(5, int(page_size)))
        q = (search or "").strip()

        where = ""
        params: Dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
        if q:
            where = "WHERE (COALESCE(title,'') ILIKE :q OR path ILIKE :q)"
            params["q"] = f"%{q}%"

        with self.sf() as s:  # type: Session
            total = s.execute(sqltext(f"SELECT COUNT(*) FROM kb_documents {where}"), params).first()
            rows = s.execute(
                sqltext(
                    f"""
                    SELECT id, title, path
                    FROM kb_documents
                    {where}
                    ORDER BY id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).all()

            ids = [int(r[0]) for r in rows]
            chunks_map: Dict[int, int] = {}
            if ids:
                cr = s.execute(
                    sqltext(
                        """
                        SELECT document_id, COUNT(*)
                        FROM kb_chunks
                        WHERE document_id = ANY(:ids)
                        GROUP BY document_id
                        """
                    ),
                    {"ids": ids},
                ).all()
                chunks_map = {int(a): int(b) for a, b in cr}

        items: List[Dict[str, Any]] = []
        for r in rows:
            did = int(r[0])
            items.append(
                {
                    "id": did,
                    "title": r[1],
                    "path": r[2],
                    "chunks": int(chunks_map.get(did, 0)),
                }
            )

        return items, int(total[0]) if total else 0

    def get_document_brief(self, document_id: int) -> Optional[Dict[str, Any]]:
        with self.sf() as s:
            row = s.execute(
                sqltext("SELECT id, title, path FROM kb_documents WHERE id=:id"),
                {"id": document_id},
            ).first()
            if not row:
                return None
            c = s.execute(
                sqltext("SELECT COUNT(*) FROM kb_chunks WHERE document_id=:id"),
                {"id": document_id},
            ).first()
        return {"id": int(row[0]), "title": row[1], "path": row[2], "chunks": int(c[0]) if c else 0}

    # ---------- stats ----------
    def stats_global(self) -> Dict[str, Any]:
        with self.sf() as s:
            docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents")).first()
            chunks = s.execute(sqltext("SELECT COUNT(*) FROM kb_chunks")).first()
            top = s.execute(
                sqltext(
                    """
                    SELECT d.id, COALESCE(d.title,''), d.path, COUNT(c.id) AS cnt
                    FROM kb_documents d
                    LEFT JOIN kb_chunks c ON c.document_id = d.id
                    GROUP BY d.id
                    ORDER BY cnt DESC
                    LIMIT 10
                    """
                )
            ).all()

        return {
            "documents": int(docs[0]) if docs else 0,
            "chunks": int(chunks[0]) if chunks else 0,
            "top_docs": [
                {"id": int(r[0]), "title": r[1], "path": r[2], "chunks": int(r[3])} for r in top
            ],
        }

    def stats_for_document_ids(self, document_ids: List[int]) -> Dict[str, Any]:
        if not document_ids:
            return {"documents": 0, "chunks": 0}
        with self.sf() as s:
            docs = s.execute(
                sqltext("SELECT COUNT(*) FROM kb_documents WHERE id = ANY(:ids)"),
                {"ids": document_ids},
            ).first()
            chunks = s.execute(
                sqltext("SELECT COUNT(*) FROM kb_chunks WHERE document_id = ANY(:ids)"),
                {"ids": document_ids},
            ).first()
        return {
            "documents": int(docs[0]) if docs else 0,
            "chunks": int(chunks[0]) if chunks else 0,
        }

    # ---------- upsert / indexing helpers ----------
    def upsert_document(self, path: str, title: str | None):
        from .models import KBDocument
        with self.sf() as s:  # type: Session
            doc = s.query(KBDocument).filter_by(path=path).first()
            if not doc:
                doc = KBDocument(path=path, title=title)
                s.add(doc); s.commit(); s.refresh(doc)
                return int(doc.id)
            if title is not None:
                doc.title = title
            s.commit(); s.refresh(doc)
            return int(doc.id)

    def delete_chunks_by_document_id(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(sqltext("DELETE FROM kb_chunks WHERE document_id=:id"), {"id": document_id})
            s.commit()

    def insert_chunk(self, document_id: int, text: str, embedding: list[float]) -> int:
        from .models import KBChunk
        with self.sf() as s:
            ch = KBChunk(document_id=document_id, text=text, embedding=dumps(embedding))
            s.add(ch); s.commit(); s.refresh(ch)
            return int(ch.id)

    # ---------- retrieval ----------
    def search_by_embedding(
        self,
        query_emb: list[float],
        top_k: int,
        allowed_document_ids: Optional[List[int]] = None,
    ) -> List[Tuple[int, str, float, int]]:
        top_k = max(1, min(20, int(top_k)))

        with self.sf() as s:
            if allowed_document_ids:
                rows = s.execute(
                    sqltext(
                        """
                        SELECT id, text, embedding, document_id
                        FROM kb_chunks
                        WHERE document_id = ANY(:ids)
                        """
                    ),
                    {"ids": allowed_document_ids},
                ).all()
            else:
                rows = s.execute(sqltext("SELECT id, text, embedding, document_id FROM kb_chunks")).all()

        def cos_sim(a, b):
            import math
            num = sum(x * y for x, y in zip(a, b))
            da = math.sqrt(sum(x * x for x in a))
            db = math.sqrt(sum(y * y for y in b))
            return num / (da * db + 1e-9)

        scored: List[Tuple[int, str, float, int]] = []
        for r in rows:
            emb = loads(r[2])
            score = cos_sim(query_emb, emb)  # больше = ближе
            distance = 1.0 - float(score)
            scored.append((int(r[0]), str(r[1]), distance, int(r[3])))

        scored.sort(key=lambda x: x[2])
        return scored[:top_k]
