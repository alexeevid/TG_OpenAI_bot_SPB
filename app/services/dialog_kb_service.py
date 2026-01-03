from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..db.repo_kb import KBRepo
from ..db.repo_dialog_kb import DialogKBRepo


class DialogKBService:
    def __init__(self, repo_dialog_kb: DialogKBRepo, repo_kb: KBRepo):
        self._dkb = repo_dialog_kb
        self._kb = repo_kb

    # --- mode ---
    def get_mode(self, dialog_id: int) -> str:
        return self._dkb.get_kb_mode(dialog_id)

    def set_mode(self, dialog_id: int, mode: str) -> str:
        self._dkb.set_kb_mode(dialog_id, mode)
        return self._dkb.get_kb_mode(dialog_id)

    def is_rag_enabled(self, dialog_id: int) -> bool:
        mode = self._dkb.get_kb_mode(dialog_id)
        if mode == "OFF":
            return False

        allowed = self._dkb.get_allowed_document_ids(dialog_id)
        if mode == "ON":
            return len(allowed) > 0

        # AUTO
        return len(allowed) > 0

    # --- attachments ---
    def list_attached(self, dialog_id: int) -> List[Dict[str, Any]]:
        return self._dkb.list_attached(dialog_id)

    def attach_by_ref(self, dialog_id: int, ref: str) -> Tuple[bool, str]:
        doc = self._kb.get_document_by_ref(ref)
        if not doc:
            return False, "Документ не найден в глобальной БЗ. Проверьте path/resource_id/id или выполните /kb sync (для админов)."
        self._dkb.attach(dialog_id, int(doc.id))
        return True, f"Подключено: {doc.title or doc.path}"

    def detach(self, dialog_id: int, doc_id: int) -> Tuple[bool, str]:
        self._dkb.detach(dialog_id, doc_id)
        return True, f"Отключено: {doc_id}"

    def enable(self, dialog_id: int, doc_id: int, enabled: bool) -> Tuple[bool, str]:
        self._dkb.enable(dialog_id, doc_id, enabled)
        return True, ("Включено" if enabled else "Исключено") + f": {doc_id}"

    def allowed_document_ids(self, dialog_id: int) -> List[int]:
        return self._dkb.get_allowed_document_ids(dialog_id)

    # --- secrets ---
    def set_pdf_password(self, dialog_id: int, doc_id: int, password: str) -> Tuple[bool, str]:
        self._dkb.set_pdf_password(dialog_id, doc_id, password)
        return True, f"Пароль сохранён для документа {doc_id} (только в рамках этого диалога)."
