from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text as sqltext


__all__ = ["DialogKBRepo"]


class DialogKBRepo:
    def __init__(self, sf):
        self.sf = sf

    # --- kb_mode in dialogs.settings (JSON/JSONB) ---
    def get_kb_mode(self, dialog_id: int) -> str:
        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext("SELECT settings FROM dialogs WHERE id=:id"),
                {"id": int(dialog_id)},
            ).first()

        settings = (row[0] if row else None) or {}
        if not isinstance(settings, dict):
            settings = {}

        mode = str(settings.get("kb_mode") or "AUTO").upper()
        return mode if mode in ("AUTO", "ON", "OFF") else "AUTO"

    def set_kb_mode(self, dialog_id: int, mode: str) -> str:
        mode_u = str(mode or "AUTO").upper()
        if mode_u not in ("AUTO", "ON", "OFF"):
            mode_u = "AUTO"

        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext("SELECT settings FROM dialogs WHERE id=:id"),
                {"id": int(dialog_id)},
            ).first()

            settings = (row[0] if row else None) or {}
            if not isinstance(settings, dict):
                settings = {}
            settings["kb_mode"] = mode_u

            # ✅ psycopg2 can't adapt dict in raw SQL params -> JSON string + cast to jsonb
            s.execute(
                sqltext("UPDATE dialogs SET settings=(:st)::jsonb WHERE id=:id"),
                {"st": json.dumps(settings, ensure_ascii=False), "id": int(dialog_id)},
            )
            s.commit()

        return mode_u

    # --- attachments (dialog_kb_documents) ---
    def is_attached(self, dialog_id: int, document_id: int) -> bool:
        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext(
                    """
                    SELECT 1
                    FROM dialog_kb_documents
                    WHERE dialog_id = :did AND document_id = :doc
                    LIMIT 1
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id)},
            ).first()
        return bool(row)

    def list_attached(self, dialog_id: int) -> List[Dict[str, Any]]:
        with self.sf() as s:  # type: Session
            rows = s.execute(
                sqltext(
                    """
                    SELECT dkd.document_id, dkd.is_enabled, kd.path, kd.title
                    FROM dialog_kb_documents dkd
                    JOIN kb_documents kd ON kd.id = dkd.document_id
                    WHERE dkd.dialog_id = :did
                    ORDER BY dkd.document_id ASC
                    """
                ),
                {"did": int(dialog_id)},
            ).fetchall()

        return [
            {"document_id": int(r[0]), "is_enabled": bool(r[1]), "path": r[2], "title": r[3]}
            for r in rows
        ]

    def attach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:  # type: Session
            s.execute(
                sqltext(
                    """
                    INSERT INTO dialog_kb_documents (dialog_id, document_id, is_enabled)
                    VALUES (:did, :doc, TRUE)
                    ON CONFLICT (dialog_id, document_id)
                    DO UPDATE SET is_enabled = TRUE
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id)},
            )
            s.commit()

    def detach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:  # type: Session
            s.execute(
                sqltext(
                    """
                    DELETE FROM dialog_kb_documents
                    WHERE dialog_id = :did AND document_id = :doc
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id)},
            )
            s.commit()

    def set_enabled(self, dialog_id: int, document_id: int, enabled: bool) -> None:
        """
        DialogKBService ожидает этот метод.
        Если строки нет — создаём её. Если есть — обновляем is_enabled.
        """
        with self.sf() as s:  # type: Session
            s.execute(
                sqltext(
                    """
                    INSERT INTO dialog_kb_documents (dialog_id, document_id, is_enabled)
                    VALUES (:did, :doc, :en)
                    ON CONFLICT (dialog_id, document_id)
                    DO UPDATE SET is_enabled = EXCLUDED.is_enabled
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id), "en": bool(enabled)},
            )
            s.commit()

    def toggle_enabled(self, dialog_id: int, document_id: int) -> bool:
        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext(
                    """
                    SELECT is_enabled
                    FROM dialog_kb_documents
                    WHERE dialog_id = :did AND document_id = :doc
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id)},
            ).first()

            if not row:
                self.attach(dialog_id, document_id)
                return True

            new_val = not bool(row[0])
            s.execute(
                sqltext(
                    """
                    UPDATE dialog_kb_documents
                    SET is_enabled = :en
                    WHERE dialog_id = :did AND document_id = :doc
                    """
                ),
                {"en": bool(new_val), "did": int(dialog_id), "doc": int(document_id)},
            )
            s.commit()
            return bool(new_val)

    def allowed_document_ids(self, dialog_id: int) -> List[int]:
        with self.sf() as s:  # type: Session
            rows = s.execute(
                sqltext(
                    """
                    SELECT document_id
                    FROM dialog_kb_documents
                    WHERE dialog_id = :did AND is_enabled = TRUE
                    ORDER BY document_id ASC
                    """
                ),
                {"did": int(dialog_id)},
            ).fetchall()
        return [int(r[0]) for r in rows]

    # ✅ Backward-compatible alias (старый код ожидает это имя)
    def get_allowed_document_ids(self, dialog_id: int) -> List[int]:
        return self.allowed_document_ids(dialog_id)

    # --- secrets (оставляем на будущее) ---
    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        with self.sf() as s:  # type: Session
            s.execute(
                sqltext(
                    """
                    INSERT INTO dialog_kb_secrets (dialog_id, document_id, pdf_password)
                    VALUES (:did, :doc, :pwd)
                    ON CONFLICT (dialog_id, document_id)
                    DO UPDATE SET pdf_password = EXCLUDED.pdf_password, updated_at = NOW()
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id), "pwd": str(password)},
            )
            s.commit()

    def get_pdf_password(self, dialog_id: int, document_id: int) -> Optional[str]:
        with self.sf() as s:  # type: Session
            row = s.execute(
                sqltext(
                    """
                    SELECT pdf_password
                    FROM dialog_kb_secrets
                    WHERE dialog_id = :did AND document_id = :doc
                    """
                ),
                {"did": int(dialog_id), "doc": int(document_id)},
            ).first()
        return str(row[0]) if row else None
