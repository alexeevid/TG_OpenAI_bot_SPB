# app/services/dialog_kb_service.py
from __future__ import annotations

from typing import Dict, List, Tuple, Optional

from ..db.repo_dialog_kb import DialogKBRepo
from ..db.repo_kb import KBRepo


class DialogKBService:
    """
    Best practice:
    - kb_mode=AUTO по умолчанию
    - AUTO: RAG включён, если есть enabled документы
    - OFF: всегда выключен
    - ON: включён, но только если есть enabled документы (иначе нечего искать)
    """

    def __init__(self, repo_dialog_kb: DialogKBRepo, repo_kb: KBRepo):
        self._dkb = repo_dialog_kb
        self._kb = repo_kb

    def get_mode(self, dialog_id: int) -> str:
        return self._dkb.get_kb_mode(dialog_id)

    def set_mode(self, dialog_id: int, mode: str) -> str:
        return self._dkb.set_kb_mode(dialog_id, mode)

    def list_attached(self, dialog_id: int) -> List[Dict]:
        return self._dkb.list_attached(dialog_id)

    def toggle_attach_enabled(self, dialog_id: int, document_id: int) -> str:
        """
        UX-toggle:
        - если не attached -> attach (enabled)
        - если attached+enabled -> disable
        - если attached+disabled -> enable
        """
        attached = self._dkb.is_attached(dialog_id, document_id)
        if not attached:
            self._dkb.attach(dialog_id, document_id)
            return "attached_enabled"

        # attached: узнаём is_enabled через список (дешево для небольшого списка)
        items = self._dkb.list_attached(dialog_id)
        cur = next((x for x in items if int(x["document_id"]) == int(document_id)), None)
        en = bool(cur["is_enabled"]) if cur else True

        if en:
            self._dkb.set_enabled(dialog_id, document_id, False)
            return "disabled"
        else:
            self._dkb.set_enabled(dialog_id, document_id, True)
            return "enabled"

    def detach(self, dialog_id: int, document_id: int) -> None:
        self._dkb.detach(dialog_id, document_id)

    def set_pdf_password(self, dialog_id: int, document_id: int, password: str) -> None:
        self._dkb.set_pdf_password(dialog_id, document_id, password)

    def allowed_document_ids(self, dialog_id: int) -> List[int]:
        return self._dkb.get_allowed_document_ids(dialog_id)

    def rag_enabled(self, dialog_id: int) -> bool:
        mode = self._dkb.get_kb_mode(dialog_id)
        if mode == "OFF":
            return False
        allowed = self._dkb.get_allowed_document_ids(dialog_id)
        return len(allowed) > 0  # для AUTO и ON одинаково: если нет доков — нечего включать
