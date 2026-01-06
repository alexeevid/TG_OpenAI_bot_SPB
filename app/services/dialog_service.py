from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..db.repo_dialogs import DialogsRepo
from ..db.models import Dialog, Message


class DialogService:
    """Единый сервис диалогов (DB-backed), используемый хендлерами."""

    def __init__(self, repo: DialogsRepo | None):
        self._repo = repo

    def _ensure_repo(self) -> DialogsRepo:
        if not self._repo:
            raise RuntimeError("DialogsRepo not configured")
        return self._repo

    def ensure_user(self, tg_user_id: str | int):
        repo = self._ensure_repo()
        return repo.ensure_user(str(tg_user_id))

    def get_active_dialog(self, tg_user_id: str | int) -> Dialog:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        d = repo.get_active_dialog(u.id)
        if d:
            return d
        # Если активного нет — создаём новый и делаем активным
        d = repo.new_dialog(u.id, title="")
        repo.set_active_dialog(u.id, d.id)
        return d

    def new_dialog(self, tg_user_id: str | int, title: str = "") -> Dialog:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        d = repo.new_dialog(u.id, title=title or "")
        repo.set_active_dialog(u.id, d.id)
        return d

    def switch_dialog(self, tg_user_id: str | int, dialog_id: int) -> bool:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            return False
        repo.set_active_dialog(u.id, d.id)
        return True

    def list_dialogs(self, tg_user_id: str | int, limit: int = 20) -> List[Dialog]:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        return repo.list_dialogs(u.id, limit=limit)

    def update_active_settings(self, tg_user_id: str | int, patch: Dict[str, Any]) -> Dialog:
        repo = self._ensure_repo()
        d = self.get_active_dialog(tg_user_id)
        updated = repo.update_dialog_settings(d.id, patch)
        return updated or d

    def get_active_settings(self, tg_user_id: str | int) -> Dict[str, Any]:
        d = self.get_active_dialog(tg_user_id)
        s = d.settings or {}
        return s if isinstance(s, dict) else {}

    def add_user_message(self, dialog_id: int, text: str) -> None:
        repo = self._ensure_repo()
        repo.add_message(dialog_id, "user", text)

    def add_assistant_message(self, dialog_id: int, text: str) -> None:
        repo = self._ensure_repo()
        repo.add_message(dialog_id, "assistant", text)

    def ensure_active_dialog(self, user_id: int):
        """
        Backward-compatible method.
        Handlers expect ensure_active_dialog(user_id) to always return a dialog.
    
        Strategy:
        1) try get_active_dialog
        2) if none -> create a new dialog and make it active (via existing APIs)
        """
        d = self.get_active_dialog(user_id)
        if d:
            return d
    
        # Try common create methods if they exist in your codebase
        if hasattr(self, "create_dialog"):
            return self.create_dialog(user_id)
    
        if hasattr(self, "create_new_dialog"):
            return self.create_new_dialog(user_id)
    
        # If service uses repo directly, fall back carefully
        if hasattr(self, "repo") and hasattr(self.repo, "create_dialog"):
            d = self.repo.create_dialog(user_id=user_id)
            # If there is a set_active method, call it
            if hasattr(self, "set_active_dialog"):
                try:
                    self.set_active_dialog(user_id, getattr(d, "id", None))
                except Exception:
                    pass
            return d
    
        raise AttributeError(
            "DialogService has no ensure_active_dialog and no compatible create method found "
            "(expected create_dialog/create_new_dialog or repo.create_dialog)."
        )

    def history(self, dialog_id: int, limit: int = 30) -> List[Message]:
        repo = self._ensure_repo()
        return repo.list_messages(dialog_id, limit=limit)
