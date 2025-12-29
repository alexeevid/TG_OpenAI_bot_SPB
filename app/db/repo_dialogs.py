from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from .models import User, Dialog, Message


class DialogsRepo:
    def __init__(self, sf):
        self.sf = sf

    # ---------- users ----------
    def ensure_user(self, tg_id: str) -> User:
        with self.sf() as s:  # type: Session
            u = s.execute(select(User).where(User.tg_id == str(tg_id))).scalars().first()
            if not u:
                u = User(tg_id=str(tg_id), role="user")
                s.add(u)
                s.commit()
                s.refresh(u)
            return u

    def get_user(self, tg_id: str) -> Optional[User]:
        with self.sf() as s:
            return s.execute(select(User).where(User.tg_id == str(tg_id))).scalars().first()

    def set_active_dialog(self, user_id: int, dialog_id: int) -> None:
        with self.sf() as s:
            u = s.get(User, user_id)
            if not u:
                return
            u.active_dialog_id = dialog_id
            s.commit()

    # ---------- dialogs ----------
    def new_dialog(self, user_id: int, title: str = "", settings: Optional[Dict[str, Any]] = None) -> Dialog:
        with self.sf() as s:
            d = Dialog(user_id=user_id, title=title or "", settings=settings or {})
            s.add(d)
            s.commit()
            s.refresh(d)
            return d

    def list_dialogs(self, user_id: int, limit: int = 20) -> List[Dialog]:
        with self.sf() as s:
            q = select(Dialog).where(Dialog.user_id == user_id).order_by(desc(Dialog.updated_at)).limit(limit)
            return list(s.execute(q).scalars().all())

    def get_dialog_for_user(self, dialog_id: int, user_id: int) -> Optional[Dialog]:
        with self.sf() as s:
            q = select(Dialog).where(Dialog.id == dialog_id, Dialog.user_id == user_id)
            return s.execute(q).scalars().first()

    def get_active_dialog(self, user_id: int) -> Optional[Dialog]:
        with self.sf() as s:
            u = s.get(User, user_id)
            if not u or not u.active_dialog_id:
                return None
            q = select(Dialog).where(Dialog.id == u.active_dialog_id, Dialog.user_id == user_id)
            return s.execute(q).scalars().first()

    def update_dialog_settings(self, dialog_id: int, patch: Dict[str, Any]) -> Optional[Dialog]:
        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return None
            base = d.settings or {}
            if not isinstance(base, dict):
                base = {}
            base.update(patch or {})
            d.settings = base
            s.commit()
            s.refresh(d)
            return d

    def rename_dialog(self, dialog_id: int, title: str) -> Optional[Dialog]:
        """Переименовать диалог."""
        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return None
            d.title = (title or "").strip()
            s.commit()
            s.refresh(d)
            return d

    def delete_dialog(self, dialog_id: int) -> None:
        """Удалить диалог (сообщения удалятся каскадом)."""
        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return

            # Если удаляем активный диалог пользователя — сбрасываем active_dialog_id.
            u = s.get(User, d.user_id)
            if u and u.active_dialog_id == dialog_id:
                u.active_dialog_id = None

            s.delete(d)
            s.commit()

    # ---------- messages ----------
    def add_message(self, dialog_id: int, role: str, content: str) -> Message:
        with self.sf() as s:
            m = Message(dialog_id=dialog_id, role=role, content=content)
            s.add(m)
            # Touch dialog to update updated_at
            d = s.get(Dialog, dialog_id)
            if d:
                d.updated_at = d.updated_at  # no-op, but forces ORM to consider update (onupdate handles)
            s.commit()
            s.refresh(m)
            return m

    def list_messages(self, dialog_id: int, limit: int = 30) -> List[Message]:
        with self.sf() as s:
            q = select(Message).where(Message.dialog_id == dialog_id).order_by(Message.id.desc()).limit(limit)
            rows = list(s.execute(q).scalars().all())
            return list(reversed(rows))
