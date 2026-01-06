# app/services/dialog_service.py
from __future__ import annotations

from typing import Any, Dict, List

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

    def ensure_active_dialog(self, tg_user_id: str | int) -> Dialog:
        """
        Backward-compatible alias.
        В этой кодовой базе get_active_dialog() уже гарантирует наличие активного диалога.
        """
        return self.get_active_dialog(tg_user_id)

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

    def history(self, dialog_id: int, limit: int = 30) -> List[Message]:
        repo = self._ensure_repo()
        return repo.list_messages(dialog_id, limit=limit)
