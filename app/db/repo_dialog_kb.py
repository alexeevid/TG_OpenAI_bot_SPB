# app/db/repo_dialog_kb.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext


class DialogKBRepo:
    """
    DB-layer для:
    - kb_mode (AUTO/ON/OFF) в dialogs.settings (JSONB)
    - dialog_kb_documents (dialog_id, document_id, is_enabled)
    - dialog_kb_secrets (dialog_id, document_id, pdf_password)
    """

    def __init__(self, sf):
        self.sf = sf

    # --- mode in dialogs.settings ---
    def get_kb_mode(self, dialog_id: int) -> str:
        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext("SELECT settings FROM dialogs WHERE id=:id"),
                {"id": dialog_id},
            ).first()
            if not row:
                return "AUTO"
            settings = row[0] or {}
            if not isinstance(settings, dict):
                return "AUTO"
            mode = str(settings.get("kb_mode") or "AUTO").upper()
            return mode if mode in ("AUTO", "ON", "OFF") else "AUTO"

    def set_kb_mode(self, dialog_id: int, mode: str) -> str:
        mode = str(mode or "AUTO").upper()
        if mode not in ("AUTO", "ON", "OFF"):
            mode = "AUTO"

        with self.sf() as s:
            row = s.execute(
                sqltext("SELECT settings FROM dialogs WHERE id=:id"),
                {"id": dialog_id},
            ).first()
            settings = (row[0] if row else None) or {}
            if not isinstance(settings, dict):
                settings = {}
            settings["kb_mode"] = mode

            s.execute(
                sqltext("UPDATE dialogs SET settings=:st WHERE id=:id"),
                {"st": settings, "id": dialog_id},
            )
            s.commit()
        return mode

    # --- attachments ---
    def list_attached(self, dialog_id: int) -> List[Dict[str, Any]]:
        with self.sf() as s:
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
                sqltext(
                    "DELETE FROM dialog_kb_documents WHERE dialog_id=:did AND document_id=:doc"
                ),
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

    def is_attached(self, dialog_id: int, document_id: int) -> bool:
        with self.sf() as s:
            row = s.execute(
                sqltext(
                    """
                    SELECT 1
                    FROM dialog_kb_documents
                    WHERE dialog_id=:did AND document_id=:doc
                    """
                ),
                {"did": dialog_id, "doc": document_id},
            ).first()
            return bool(row)

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

    # --- secrets (PDF passwords per dialog) ---
    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        password = (password or "").strip()
        if not password:
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
                {"did": dialog_id, "doc": document_id, "pwd": password},
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
