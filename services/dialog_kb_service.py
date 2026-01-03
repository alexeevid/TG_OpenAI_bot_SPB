from __future__ import annotations

from typing import Dict, List

from ..db.repo_dialog_kb import DialogKBRepo
from ..db.repo_kb import KBRepo


class DialogKBService:
    """Управление тем, какие документы БЗ участвуют в контексте конкретного диалога."""

    def __init__(self, repo_dialog_kb: DialogKBRepo, repo_kb: KBRepo):
        self._dkb = repo_dialog_kb
        self._kb = repo_kb

    def list_attached(self, dialog_id: int) -> List[Dict]:
        return self._dkb.list_attached(dialog_id)

    def allowed_document_ids(self, dialog_id: int) -> List[int]:
        return self._dkb.get_allowed_document_ids(dialog_id)

    def toggle(self, dialog_id: int, document_id: int) -> str:
        """
        UX-toggle:
        - not attached -> attach(enabled)
        - attached+enabled -> disable
        - attached+disabled -> enable
        """
        attached = self._dkb.is_attached(dialog_id, document_id)
        if not attached:
            self._dkb.attach(dialog_id, document_id)
            return "attached_enabled"

        items = self._dkb.list_attached(dialog_id)
        cur = next((x for x in items if int(x["document_id"]) == int(document_id)), None)
        en = bool(cur["is_enabled"]) if cur else True

        self._dkb.set_enabled(dialog_id, document_id, not en)
        return "enabled" if not en else "disabled"

    def detach(self, dialog_id: int, document_id: int) -> None:
        self._dkb.detach(dialog_id, document_id)

    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        self._dkb.set_pdf_password(dialog_id, document_id, password)

    def stats_dialog(self, dialog_id: int) -> Dict:
        ids = self.allowed_document_ids(dialog_id)
        st = self._kb.stats_for_document_ids(ids)
        attached = self._dkb.list_attached(dialog_id)
        enabled = sum(1 for x in attached if x.get("is_enabled"))
        return {
            "attached": len(attached),
            "enabled": enabled,
            "documents_in_scope": st.get("documents", 0),
            "chunks_in_scope": st.get("chunks", 0),
        }
