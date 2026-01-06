# app/services/dialog_service.py
from __future__ import annotations

from typing import Any, Dict, List, Literal, Tuple

from ..db.repo_dialogs import DialogsRepo
from ..db.models import Dialog, Message


ModelKind = Literal["text", "image", "transcribe"]


class DialogService:
    """Единый сервис диалогов (DB-backed), используемый хендлерами."""

    def __init__(self, repo: DialogsRepo | None, settings: Any = None):
        self._repo = repo
        # settings не обязателен, но полезен для дефолтов
        self._settings = settings

    def _ensure_repo(self) -> DialogsRepo:
        if not self._repo:
            raise RuntimeError("DialogsRepo not configured")
        return self._repo

    # -------- defaults / models --------
    def _default_text_model(self) -> str:
        cfg = self._settings
        if cfg is not None:
            return getattr(cfg, "text_model", None) or getattr(cfg, "openai_text_model", None) or "gpt-4o-mini"
        return "gpt-4o-mini"

    def _default_image_model(self) -> str:
        cfg = self._settings
        if cfg is not None:
            return getattr(cfg, "image_model", None) or getattr(cfg, "openai_image_model", None) or "gpt-image-1"
        return "gpt-image-1"

    def _default_transcribe_model(self) -> str:
        cfg = self._settings
        if cfg is not None:
            return (
                getattr(cfg, "transcribe_model", None)
                or getattr(cfg, "openai_transcribe_model", None)
                or "whisper-1"
            )
        return "whisper-1"

    def _ensure_settings_shape(self, d: Dialog) -> Dict[str, Any]:
        """
        Гарантирует, что dialog.settings — dict и в нём есть ключи моделей по модальностям.
        Если чего-то нет — дописывает дефолты в БД (одним patch).
        """
        raw = d.settings or {}
        s: Dict[str, Any] = raw if isinstance(raw, dict) else {}

        patch: Dict[str, Any] = {}
        if not s.get("text_model"):
            patch["text_model"] = self._default_text_model()
        if not s.get("image_model"):
            patch["image_model"] = self._default_image_model()
        if not s.get("transcribe_model"):
            patch["transcribe_model"] = self._default_transcribe_model()

        if patch:
            repo = self._ensure_repo()
            updated = repo.update_dialog_settings(d.id, patch)
            if updated and isinstance(updated.settings, dict):
                return updated.settings
            # если repo вернул None — применяем локально, чтобы не падать
            s = dict(s)
            s.update(patch)

        return s

    def ensure_user(self, tg_user_id: str | int):
        repo = self._ensure_repo()
        return repo.ensure_user(str(tg_user_id))

    def get_active_dialog(self, tg_user_id: str | int) -> Dialog:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        d = repo.get_active_dialog(u.id)
        if d:
            # гарантируем структуру settings + дефолты моделей
            _ = self._ensure_settings_shape(d)
            return d
        # Если активного нет — создаём новый и делаем активным
        d = repo.new_dialog(u.id, title="")
        repo.set_active_dialog(u.id, d.id)
        _ = self._ensure_settings_shape(d)
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
        _ = self._ensure_settings_shape(d)
        return d

    def switch_dialog(self, tg_user_id: str | int, dialog_id: int) -> bool:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        d = repo.get_dialog_for_user(dialog_id, u.id)
        if not d:
            return False
        repo.set_active_dialog(u.id, d.id)
        _ = self._ensure_settings_shape(d)
        return True

    def list_dialogs(self, tg_user_id: str | int, limit: int = 20) -> List[Dialog]:
        repo = self._ensure_repo()
        u = repo.ensure_user(str(tg_user_id))
        return repo.list_dialogs(u.id, limit=limit)

    def update_active_settings(self, tg_user_id: str | int, patch: Dict[str, Any]) -> Dialog:
        repo = self._ensure_repo()
        d = self.get_active_dialog(tg_user_id)

        # Защитим модели: если кто-то кладёт пустое значение — не портим настройки
        safe_patch = dict(patch or {})
        if "text_model" in safe_patch and not safe_patch["text_model"]:
            safe_patch.pop("text_model", None)
        if "image_model" in safe_patch and not safe_patch["image_model"]:
            safe_patch.pop("image_model", None)
        if "transcribe_model" in safe_patch and not safe_patch["transcribe_model"]:
            safe_patch.pop("transcribe_model", None)

        updated = repo.update_dialog_settings(d.id, safe_patch)
        d2 = updated or d
        # после обновления снова гарантируем целостность settings
        _ = self._ensure_settings_shape(d2)
        return d2

    def get_active_settings(self, tg_user_id: str | int) -> Dict[str, Any]:
        d = self.get_active_dialog(tg_user_id)
        return self._ensure_settings_shape(d)

    # -------- models convenience API --------
    def get_active_models(self, tg_user_id: str | int) -> Dict[str, str]:
        """
        Удобный метод: вернуть модели по модальностям из активного диалога.
        """
        s = self.get_active_settings(tg_user_id)
        return {
            "text_model": str(s.get("text_model") or self._default_text_model()),
            "image_model": str(s.get("image_model") or self._default_image_model()),
            "transcribe_model": str(s.get("transcribe_model") or self._default_transcribe_model()),
        }

    def set_active_model(self, tg_user_id: str | int, kind: ModelKind, model: str) -> Dialog:
        """
        Установить модель по конкретной модальности в активном диалоге.
        """
        key = f"{kind}_model"
        return self.update_active_settings(tg_user_id, {key: model})

    def add_user_message(self, dialog_id: int, text: str) -> None:
        repo = self._ensure_repo()
        repo.add_message(dialog_id, "user", text)

    def add_assistant_message(self, dialog_id: int, text: str) -> None:
        repo = self._ensure_repo()
        repo.add_message(dialog_id, "assistant", text)

    def history(self, dialog_id: int, limit: int = 30) -> List[Message]:
        repo = self._ensure_repo()
        return repo.list_messages(dialog_id, limit=limit)
