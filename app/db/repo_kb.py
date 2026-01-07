from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
from datetime import datetime

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

    def list_documents_brief(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """
        Минимальный набор полей для служебных операций (scan/sync).
        """
        where = "WHERE is_active=TRUE" if active_only else ""
        with self.sf() as s:
            rows = s.execute(
                sqltext(
                    f"""
                    SELECT id, path, md5, size, modified_at, indexed_at, status, is_active
                    FROM kb_documents
                    {where}
                    """
                )
            ).fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0]),
                    "path": r[1],
                    "md5": r[2],
                    "size": int(r[3]) if r[3] is not None else None,
                    "modified_at": r[4],
                    "indexed_at": r[5],
                    "status": r[6],
                    "is_active": bool(r[7]),
                }
            )
        return out

    def catalog(
        self,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        off = (page - 1) * page_size

        q = (search or "").strip()
        where = "WHERE d.is_active=TRUE"
        params: Dict[str, Any] = {"off": off, "lim": page_size}
        if q:
            where += " AND (d.title ILIKE :q OR d.path ILIKE :q)"
            params["q"] = f"%{q}%"

        with self.sf() as s:
            total_row = s.execute(
                sqltext(f"SELECT COUNT(*) FROM kb_documents d {where}"),
                params if q else None,
            ).first()
            total = int(total_row[0]) if total_row else 0

            rows = s.execute(
                sqltext(
                    f"""
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
                    {where}
                    GROUP BY d.id
                    ORDER BY d.path ASC
                    OFFSET :off LIMIT :lim
                    """
                ),
                params,
            ).fetchall()

        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": int(r[0]),
                    "title": (r[1] or "").strip() or None,
                    "path": r[2],
                    "status": r[3],
                    "indexed_at": r[4],
                    "last_error": r[5],
                    "chunks": int(r[6]),
                }
            )
        return items, total

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
        """
        Формат совместим с handlers/kb.py:
          { documents, chunks, top_docs:[{id,title,path,chunks}] }
        """
        with self.sf() as s:
            docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE")).first()
            chunks = s.execute(sqltext("SELECT COUNT(*) FROM kb_chunks")).first()
            top = s.execute(
                sqltext(
                    """
                    SELECT d.id, COALESCE(d.title, '') AS title, d.path, COUNT(c.id) AS cnt
                    FROM kb_documents d
                    LEFT JOIN kb_chunks c ON c.document_id = d.id
                    WHERE d.is_active=TRUE
                    GROUP BY d.id
                    ORDER BY cnt DESC
                    LIMIT 10
                    """
                )
            ).fetchall()

        return {
            "documents": int(docs[0]) if docs else 0,
            "chunks": int(chunks[0]) if chunks else 0,
            "top_docs": [
                {
                    "id": int(r[0]),
                    "title": (r[1] or "").strip() or None,
                    "path": r[2],
                    "chunks": int(r[3]),
                }
                for r in top
            ],
        }

    def stats_for_document_ids(self, document_ids: Sequence[int]) -> Dict[str, Any]:
        """
        Агрегаты для текущего диалога:
          { documents: <сколько документов в scope>, chunks: <сколько чанков в scope> }
        """
        ids = [int(x) for x in document_ids]
        if not ids:
            return {"documents": 0, "chunks": 0}

        with self.sf() as s:
            docs_row = s.execute(
                sqltext(
                    """
                    SELECT COUNT(*)
                    FROM kb_documents
                    WHERE is_active=TRUE AND id = ANY(:ids)
                    """
                ),
                {"ids": ids},
            ).first()
            chunks_row = s.execute(
                sqltext(
                    """
                    SELECT COUNT(*)
                    FROM kb_chunks
                    WHERE document_id = ANY(:ids)
                    """
                ),
                {"ids": ids},
            ).first()

        return {
            "documents": int(docs_row[0]) if docs_row else 0,
            "chunks": int(chunks_row[0]) if chunks_row else 0,
        }

    def status_summary(self) -> Dict[str, Any]:
        """
        Для /kb status (админ): показывает “здоровье” БЗ.
        """
        with self.sf() as s:
            active_docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE")).first()
            all_docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents")).first()
            chunks = s.execute(sqltext("SELECT COUNT(*) FROM kb_chunks")).first()

            last_indexed = s.execute(sqltext("SELECT MAX(indexed_at) FROM kb_documents WHERE indexed_at IS NOT NULL")).first()
            err_docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE AND status='error'")).first()
            skipped_docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE AND status='skipped'")).first()
            indexed_docs = s.execute(sqltext("SELECT COUNT(*) FROM kb_documents WHERE is_active=TRUE AND status='indexed'")).first()

        return {
            "documents_active": int(active_docs[0]) if active_docs else 0,
            "documents_total": int(all_docs[0]) if all_docs else 0,
            "chunks_total": int(chunks[0]) if chunks else 0,
            "documents_indexed": int(indexed_docs[0]) if indexed_docs else 0,
            "documents_skipped": int(skipped_docs[0]) if skipped_docs else 0,
            "documents_error": int(err_docs[0]) if err_docs else 0,
            "last_indexed_at": last_indexed[0] if last_indexed else None,
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
            if last_error is not None:
                doc.last_error = last_error

            s.commit()
            return int(doc.id)

    def mark_all_documents_inactive(self) -> None:
        with self.sf() as s:
            s.execute(sqltext("UPDATE kb_documents SET is_active=FALSE"))
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
                    SELECT md5, modified_at, size, indexed_at
                    FROM kb_documents
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id)},
            ).first()
            if not row:
                return True
            old_md5, old_modified, old_size, indexed_at = row

        if indexed_at is None:
            return True

        if md5 and old_md5 and md5 != old_md5:
            return True
        if size is not None and old_size is not None and int(size) != int(old_size):
            return True
        if modified_at is not None and old_modified is not None and modified_at != old_modified:
            return True

        if old_md5 is None and md5 is not None:
            return True
        if old_size is None and size is not None:
            return True
        if old_modified is None and modified_at is not None:
            return True

        return False

    def set_document_status(self, document_id: int, *, status: str, last_error: str | None = None) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_documents
                    SET status=:status, last_error=:err
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id), "status": status, "err": last_error},
            )
            s.commit()

    def set_document_indexed(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE kb_documents
                    SET indexed_at=NOW(), status='indexed', last_error=NULL
                    WHERE id=:id
                    """
                ),
                {"id": int(document_id)},
            )
            s.commit()

    # ----------------------------
    # Chunks / embeddings
    # ----------------------------
    def delete_chunks_by_document_id(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(sqltext("DELETE FROM kb_chunks WHERE document_id=:id"), {"id": int(document_id)})
            s.commit()

    def insert_chunks_bulk(self, rows: Sequence[Tuple[int, int, str, list[float]]]) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    INSERT INTO kb_chunks(document_id, chunk_order, text, embedding)
                    VALUES (:document_id, :chunk_order, :text, :embedding)
                    """
                ),
                [
                    {
                        "document_id": int(did),
                        "chunk_order": int(order),
                        "text": text,
                        "embedding": emb,
                    }
                    for (did, order, text, emb) in rows
                ],
            )
            s.commit()

    def search_by_embedding(self, query_vector: list[float], *, limit: int = 6, document_ids: Sequence[int] | None = None):
        # IMPORTANT:
        # psycopg2 адаптирует list[float] как numeric[], а pgvector operator <=> ожидает vector.
        # Поэтому передаем строковый литерал вида '[1,2,3,...]' и явно кастим к ::vector.
        vec_literal = "[" + ",".join(f"{float(x):.10g}" for x in query_vector) + "]"

        params: Dict[str, Any] = {"q": vec_literal, "lim": int(limit)}
        where = ""
        if document_ids:
            params["ids"] = [int(x) for x in document_ids]
            where = "WHERE document_id = ANY(:ids)"

        with self.sf() as s:
            rows = s.execute(
                sqltext(
                    f"""
                    SELECT id, document_id, chunk_order, text,
                           1 - (embedding <=> (:q)::vector) AS score
                    FROM kb_chunks
                    {where}
                    ORDER BY embedding <=> (:q)::vector
                    LIMIT :lim
                    """
                ),
                params,
            ).fetchall()

        out = []
        for r in rows:
            out.append(
                {
                    "chunk_id": int(r[0]),
                    "document_id": int(r[1]),
                    "chunk_order": int(r[2]),
                    "text": r[3],
                    "score": float(r[4]),
                }
            )
        return out
