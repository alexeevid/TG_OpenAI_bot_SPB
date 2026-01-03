from __future__ import annotations

from typing import List, Tuple, Optional, Sequence
from json import dumps, loads

from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext

from .models import KBDocument


class KBRepo:
    def __init__(self, sf, dim: int):
        self.sf = sf
        self.dim = dim

    def upsert_document(
        self,
        *,
        path: str,
        title: str | None = None,
        resource_id: str | None = None,
        md5: str | None = None,
        size: int | None = None,
        modified_at=None,
        is_active: bool = True,
    ) -> int:
        with self.sf() as s:  # type: Session
            doc = s.query(KBDocument).filter_by(path=path).first()
            if not doc and resource_id:
                doc = s.query(KBDocument).filter_by(resource_id=resource_id).first()

            if not doc:
                doc = KBDocument(
                    path=path,
                    title=title,
                    resource_id=resource_id,
                    md5=md5,
                    size=size,
                    modified_at=modified_at,
                    is_active=is_active,
                )
                s.add(doc)
                s.commit()
                s.refresh(doc)
                return int(doc.id)

            # update
            doc.path = path
            if title is not None:
                doc.title = title
            if resource_id is not None:
                doc.resource_id = resource_id
            if md5 is not None:
                doc.md5 = md5
            if size is not None:
                doc.size = size
            if modified_at is not None:
                doc.modified_at = modified_at
            doc.is_active = bool(is_active)

            s.commit()
            s.refresh(doc)
            return int(doc.id)

    def set_document_active(self, document_id: int, is_active: bool) -> None:
        with self.sf() as s:
            doc = s.get(KBDocument, document_id)
            if not doc:
                return
            doc.is_active = bool(is_active)
            s.commit()

    def get_document_by_ref(self, ref: str) -> Optional[KBDocument]:
        """
        ref может быть:
        - числом (id)
        - resource_id
        - path (полный)
        """
        ref = (ref or "").strip()
        if not ref:
            return None

        with self.sf() as s:
            if ref.isdigit():
                return s.get(KBDocument, int(ref))

            doc = s.query(KBDocument).filter_by(resource_id=ref).first()
            if doc:
                return doc

            return s.query(KBDocument).filter_by(path=ref).first()

    def list_documents(self, only_active: bool = True) -> list[KBDocument]:
        with self.sf() as s:
            q = s.query(KBDocument)
            if only_active:
                q = q.filter_by(is_active=True)
            return q.order_by(KBDocument.updated_at.desc()).all()

    def delete_chunks_by_document_id(self, document_id: int) -> None:
        with self.sf() as s:
            s.execute(sqltext("DELETE FROM kb_chunks WHERE document_id = :id"), {"id": document_id})
            s.commit()

    def insert_chunks_bulk(self, rows: Sequence[tuple[int, int, str, list[float]]]) -> None:
        """
        rows: (document_id, chunk_order, text, embedding_list)
        embedding хранится как JSON-строка в kb_chunks.embedding
        """
        payload = []
        for (doc_id, order, text, emb) in rows:
            payload.append(
                {
                    "document_id": doc_id,
                    "chunk_order": order,
                    "text": text,
                    "embedding": dumps(emb),
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

    def search_by_embedding(
        self,
        query_emb: list[float],
        top_k: int,
        allowed_document_ids: Optional[list[int]] = None,
    ) -> List[Tuple[int, str, float, int]]:
        """
        Возвращает: (chunk_id, text, distance, document_id)

        distance — условная “дистанция” (меньше = лучше), как у вас в text.py.
        """
        # Подтянем только нужные чанки
        if allowed_document_ids:
            with self.sf() as s:
                rows = s.execute(
                    sqltext(
                        """
                        SELECT id, text, embedding, document_id
                        FROM kb_chunks
                        WHERE document_id = ANY(:doc_ids)
                        """
                    ),
                    {"doc_ids": allowed_document_ids},
                ).all()
        else:
            with self.sf() as s:
                rows = s.execute(sqltext("SELECT id, text, embedding, document_id FROM kb_chunks")).all()

        def cos_sim(a, b):
            import math
            num = sum(x * y for x, y in zip(a, b))
            da = math.sqrt(sum(x * x for x in a))
            db = math.sqrt(sum(y * y for y in b))
            return num / (da * db + 1e-9)

        scored: list[tuple[int, str, float, int]] = []
        for r in rows:
            emb = loads(r[2])
            score = cos_sim(query_emb, emb)  # больше = ближе
            distance = 1.0 - score
            scored.append((int(r[0]), str(r[1]), float(distance), int(r[3])))

        scored.sort(key=lambda x: x[2])
        return scored[:top_k]
