from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext


class KBRepo:
    """Repository for KB documents and pgvector-backed chunks."""

    def __init__(self, session_factory, dim: int):
        self.sf = session_factory
        self.dim = int(dim)

    # ----------------------------
    # Documents
    # ----------------------------
    def list_documents_total(self) -> int:
        with self.sf() as s:
            row = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE")).first()
            return int(row[0]) if row else 0

    def catalog(self, page: int = 1, page_size: int = 20) -> Tuple[List[Dict[str, Any]], int]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        off = (page - 1) * page_size

        with self.sf() as s:
            total = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE")).first()
            rows = s.execute(
                sqltext(
                    """
                    SELECT
                        d.id,
                        COALESCE(d.title, '') AS title,
                        d.path,
                        d.status,
                        d.indexed_at,
                        d.last_error,
                        COUNT(c.id) AS chunks
                    FROM kb_documents d
                    LEFT JOIN kb_chunks c ON c.document_id = d.id
                    WHERE d.is_active=TRUE
                    GROUP BY d.id
                    ORDER BY d.path ASC
                    OFFSET :off LIMIT :lim
                    """
                ),
                {"off": off, "lim": page_size},
            ).all()

        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": int(r[0]),
                    "title": r[1] or None,
                    "path": r[2],
                    "status": r[3],
                    "indexed_at": r[4],
                    "last_error": r[5],
                    "chunks": int(r[6]),
                }
            )
        return items, int(total[0]) if total else 0

    def get_document_brief(self, document_id: int) -> Optional[Dict[str, Any]]:
        with self.sf() as s:
            row = s.execute(
                sqltext("SELECT id, title, path FROM kb_documents WHERE id=:id"),
                {"id": int(document_id)},
            ).first()
        if not row:
            return None
        return {"id": int(row[0]), "title": row[1], "path": row[2]}

    def stats_global(self) -> Dict[str, Any]:
        with self.sf() as s:
            docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE")).first()
            chunks = s.execute(sqltext("SELECT COUNT(*) FROM kb_chunks")).first()
            top = s.execute(
                sqltext(
                    """
                    SELECT d.id, COALESCE(d.title,''), d.path, COUNT(c.id) AS cnt
                    FROM kb_documents d
                    LEFT JOIN kb_chunks c ON c.document_id = d.id
                    WHERE d.is_active=TRUE
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

    def upsert_document(
        self,
        path: str,
        title: str | None,
        *,
        resource_id: str | None = None,
        md5: str | None = None,
        size: int | None = None,
        modified_at: datetime | None = None,
        is_active: bool = True,
        status: str | None = None,
        last_error: str | None = None,
    ) -> int:
        from .models import KBDocument

        with self.sf() as s:  # type: Session
            doc = s.query(KBDocument).filter_by(path=path).first()
            if not doc:
                doc = KBDocument(path=path, title=title)
                s.add(doc)
                s.flush()

            if title is not None:
                doc.title = title
            if resource_id is not None:
                doc.resource_id = resource_id
            if md5 is not None:
                doc.md5 = md5
            if size is not None:
                doc.size = int(size)
            if modified_at is not None:
                doc.modified_at = modified_at

            doc.is_active = bool(is_active)

            if status is not None:
                doc.status = status
                if status == "indexed":
                    doc.indexed_at = sa.func.now()
            if last_error is not None:
                doc.last_error = last_error

            s.commit()
            s.refresh(doc)
            return int(doc.id)

    def mark_all_documents_inactive(self) -> None:
        with self.sf() as s:
            s.execute(sqltext("UPDATE kb_documents SET is_active=FALSE WHERE is_active=TRUE"))
            s.commit()

    def document_needs_reindex(
        self,
        document_id: int,
        *,
        md5: str | None,
        modified_at: datetime | None,
        size: int | None,
    ) -> bool:
        with self.sf() as s:
            row = s.execute(
                sqltext(
                    """
                    SELECT md5, modified_at, size,
                           (SELECT COUNT(*) FROM kb_chunks WHERE document_id = :id) AS chunks
                    FROM kb_documents
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id)},
            ).first()
            if not row:
                return True

            db_md5, db_mod, db_size, chunks = row[0], row[1], row[2], row[3]
            if not chunks or int(chunks) == 0:
                return True

            if md5 and db_md5 and str(md5) != str(db_md5):
                return True
            if size is not None and db_size is not None and int(size) != int(db_size):
                return True
            if modified_at is not None and db_mod is not None:
                try:
                    if modified_at != db_mod:
                        return True
                except Exception:
                    pass
            return False

    def set_document_status(self, document_id: int, *, status: str, last_error: str | None) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_documents
                    SET status=:st,
                        last_error=:err,
                        indexed_at=CASE WHEN :st='indexed' THEN NOW() ELSE indexed_at END,
                        updated_at=NOW()
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id), "st": status, "err": last_error},
            )
            s.commit()

    def set_document_indexed(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_documents
                    SET status='indexed',
                        indexed_at=NOW(),
                        last_error=NULL,
                        updated_at=NOW()
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id)},
            )
            s.commit()

    # ----------------------------
    # Chunks
    # ----------------------------
    def delete_chunks_by_document_id(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(
                sqltext("DELETE FROM kb_chunks WHERE document_id=:id"),
                {"id": int(document_id)},
            )
            s.commit()

    def insert_chunks_bulk(self, rows: Sequence[Tuple[int, int, str, list[float]]]) -> None:
        payload = []
        for (doc_id, order, text, emb) in rows:
            payload.append(
                {
                    "document_id": int(doc_id),
                    "chunk_order": int(order),
                    "text": text,
                    "embedding": emb,
                }
            )

        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    INSERT INTO kb_chunks (document_id, chunk_order, text, embedding)
                    VALUES (:document_id, :chunk_order, :text, :embedding)
                    """
                ),
                payload,
            )
            s.commit()

    # ----------------------------
    # Retrieval
    # ----------------------------
    def search_by_embedding(
        self,
        query_emb: list[float],
        top_k: int,
        allowed_document_ids: Optional[List[int]] = None,
    ) -> List[Tuple[int, str, float, int]]:
        params: Dict[str, Any] = {"q": query_emb, "k": int(top_k)}
        where = "1=1"
        if allowed_document_ids:
            where += " AND c.document_id = ANY(:doc_ids)"
            params["doc_ids"] = allowed_document_ids

        sql = f"""
            SELECT
                c.id,
                c.text,
                (1 - (c.embedding <=> :q)) AS similarity,
                c.document_id
            FROM kb_chunks c
            WHERE {where}
            ORDER BY c.embedding <=> :q
            LIMIT :k
        """

        with self.sf() as s:
            rows = s.execute(sqltext(sql), params).all()

        out: List[Tuple[int, str, float, int]] = []
        for r in rows:
            out.append((int(r[0]), str(r[1]), float(r[2]), int(r[3])))
        return out
