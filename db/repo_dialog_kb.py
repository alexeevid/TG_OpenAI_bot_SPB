from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext


class DialogKBRepo:
    """DB слой для привязки документов БЗ к конкретному диалогу."""

    def __init__(self, sf):
        self.sf = sf

    def list_attached(self, dialog_id: int) -> List[Dict[str, Any]]:
        with self.sf() as s:  # type: Session
            rows = s.execute(
                sqltext(
                    """
                    SELECT dkd.document_id, dkd.is_enabled,
                           kd.path, kd.title
                    FROM dialog_kb_documents dkd
                    JOIN kb_documents kd ON kd.id = dkd.document_id
                    WHERE dkd.dialog_id = :did
                    ORDER BY dkd.document_id ASC
                    """
                ),
                {"did": dialog_id},
            ).all()

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "document_id": int(r[0]),
                    "is_enabled": bool(r[1]),
                    "path": r[2],
                    "title": r[3],
                }
            )
        return out

    def is_attached(self, dialog_id: int, document_id: int) -> bool:
        with self.sf() as s:
            row = s.execute(
                sqltext(
                    """
                    SELECT 1 FROM dialog_kb_documents
                    WHERE dialog_id=:did AND document_id=:doc
                    """
                ),
                {"did": dialog_id, "doc": document_id},
            ).first()
        return bool(row)

    def attach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    INSERT INTO dialog_kb_documents (dialog_id, document_id, is_enabled)
                    VALUES (:did, :doc, TRUE)
                    ON CONFLICT (dialog_id, document_id)
                    DO UPDATE SET is_enabled = TRUE
                    """
                ),
                {"did": dialog_id, "doc": document_id},
            )
            s.commit()

    def detach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:
            s.execute(
                sqltext("DELETE FROM dialog_kb_documents WHERE dialog_id=:did AND document_id=:doc"),
                {"did": dialog_id, "doc": document_id},
            )
            s.commit()

    def set_enabled(self, dialog_id: int, document_id: int, enabled: bool) -> None:
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    UPDATE dialog_kb_documents
                    SET is_enabled=:en
                    WHERE dialog_id=:did AND document_id=:doc
                    """
                ),
                {"did": dialog_id, "doc": document_id, "en": bool(enabled)},
            )
            s.commit()

    def get_allowed_document_ids(self, dialog_id: int) -> List[int]:
        with self.sf() as s:
            rows = s.execute(
                sqltext(
                    """
                    SELECT document_id
                    FROM dialog_kb_documents
                    WHERE dialog_id=:did AND is_enabled=TRUE
                    ORDER BY document_id ASC
                    """
                ),
                {"did": dialog_id},
            ).all()
        return [int(r[0]) for r in rows]

    # --- PDF passwords per dialog (optional, forward compatible) ---
    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        pwd = (password or "").strip()
        if not pwd:
            return
        with self.sf() as s:
            s.execute(
                sqltext(
                    """
                    INSERT INTO dialog_kb_secrets (dialog_id, document_id, pdf_password)
                    VALUES (:did, :doc, :pwd)
                    ON CONFLICT (dialog_id, document_id)
                    DO UPDATE SET pdf_password=EXCLUDED.pdf_password, updated_at=NOW()
                    """
                ),
                {"did": dialog_id, "doc": document_id, "pwd": pwd},
            )
            s.commit()

    def get_pdf_password(self, dialog_id: int, document_id: int) -> Optional[str]:
        with self.sf() as s:
            row = s.execute(
                sqltext(
                    """
                    SELECT pdf_password
                    FROM dialog_kb_secrets
                    WHERE dialog_id=:did AND document_id=:doc
                    """
                ),
                {"did": dialog_id, "doc": document_id},
            ).first()
        return str(row[0]) if row else None
