from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_

from .models import Dialog, KBDocument, DialogKBDocument, DialogKBSecret


class DialogKBRepo:
    def __init__(self, sf):
        self.sf = sf

    # --- kb_mode in Dialog.settings ---
    def get_kb_mode(self, dialog_id: int) -> str:
        with self.sf() as s:  # type: Session
            d = s.get(Dialog, dialog_id)
            if not d:
                return "AUTO"
            settings = d.settings or {}
            if not isinstance(settings, dict):
                return "AUTO"
            mode = (settings.get("kb_mode") or "AUTO").upper()
            return mode if mode in ("AUTO", "ON", "OFF") else "AUTO"

    def set_kb_mode(self, dialog_id: int, mode: str) -> None:
        mode = (mode or "AUTO").upper()
        if mode not in ("AUTO", "ON", "OFF"):
            mode = "AUTO"

        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return
            base = d.settings or {}
            if not isinstance(base, dict):
                base = {}
            base["kb_mode"] = mode
            d.settings = base
            s.commit()

    # --- attachments ---
    def list_attached(self, dialog_id: int) -> List[Dict[str, Any]]:
        with self.sf() as s:
            rows = (
                s.query(DialogKBDocument, KBDocument)
                .join(KBDocument, KBDocument.id == DialogKBDocument.document_id)
                .filter(DialogKBDocument.dialog_id == dialog_id)
                .order_by(DialogKBDocument.created_at.desc())
                .all()
            )
            out: List[Dict[str, Any]] = []
            for link, doc in rows:
                out.append(
                    {
                        "document_id": int(doc.id),
                        "path": doc.path,
                        "title": doc.title,
                        "resource_id": doc.resource_id,
                        "is_active": bool(doc.is_active),
                        "is_enabled": bool(link.is_enabled),
                    }
                )
            return out

    def attach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:
            existing = (
                s.query(DialogKBDocument)
                .filter(and_(DialogKBDocument.dialog_id == dialog_id, DialogKBDocument.document_id == document_id))
                .first()
            )
            if existing:
                existing.is_enabled = True
                s.commit()
                return
            s.add(DialogKBDocument(dialog_id=dialog_id, document_id=document_id, is_enabled=True))
            s.commit()

    def detach(self, dialog_id: int, document_id: int) -> None:
        with self.sf() as s:
            row = (
                s.query(DialogKBDocument)
                .filter(and_(DialogKBDocument.dialog_id == dialog_id, DialogKBDocument.document_id == document_id))
                .first()
            )
            if not row:
                return
            s.delete(row)
            s.commit()

    def enable(self, dialog_id: int, document_id: int, enabled: bool) -> None:
        with self.sf() as s:
            row = (
                s.query(DialogKBDocument)
                .filter(and_(DialogKBDocument.dialog_id == dialog_id, DialogKBDocument.document_id == document_id))
                .first()
            )
            if not row:
                return
            row.is_enabled = bool(enabled)
            s.commit()

    def get_allowed_document_ids(self, dialog_id: int) -> List[int]:
        with self.sf() as s:
            rows = (
                s.query(DialogKBDocument.document_id)
                .join(KBDocument, KBDocument.id == DialogKBDocument.document_id)
                .filter(DialogKBDocument.dialog_id == dialog_id)
                .filter(DialogKBDocument.is_enabled.is_(True))
                .filter(KBDocument.is_active.is_(True))
                .all()
            )
            return [int(r[0]) for r in rows]

    # --- secrets (pdf passwords) ---
    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        password = (password or "").strip()
        if not password:
            return

        with self.sf() as s:
            row = (
                s.query(DialogKBSecret)
                .filter(and_(DialogKBSecret.dialog_id == dialog_id, DialogKBSecret.document_id == document_id))
                .first()
            )
            if row:
                row.pdf_password = password
                s.commit()
                return
            s.add(DialogKBSecret(dialog_id=dialog_id, document_id=document_id, pdf_password=password))
            s.commit()

    def get_pdf_password(self, dialog_id: int, document_id: int) -> Optional[str]:
        with self.sf() as s:
            row = (
                s.query(DialogKBSecret)
                .filter(and_(DialogKBSecret.dialog_id == dialog_id, DialogKBSecret.document_id == document_id))
                .first()
            )
            return row.pdf_password if row else None
