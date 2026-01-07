from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select, desc, nullslast, func
from sqlalchemy.orm import Session

from .models import User, Dialog, Message


__all__ = ["DialogsRepo"]


class DialogsRepo:
    def __init__(self, sf):
        self.sf = sf

    # ---------- users ----------
    def ensure_user(self, tg_id: str) -> User:
        with self.sf() as s:  # type: Session
            u = (
                s.execute(select(User).where(User.tg_id == str(tg_id)))
                .scalars()
                .first()
            )
            if not u:
                u = User(tg_id=str(tg_id), role="user")
                s.add(u)
                s.commit()
                s.refresh(u)

            # ensure active dialog
            if u.active_dialog_id is None:
                d = (
                    s.execute(
                        select(Dialog)
                        .where(Dialog.user_id == u.id)
                        .order_by(Dialog.id.asc())
                    )
                    .scalars()
                    .first()
                )
                if not d:
                    d = Dialog(user_id=u.id, title="", settings={})
                    s.add(d)
                    s.commit()
                    s.refresh(d)

                    # имя по шаблону "date + brief title"
                    d.title = f"{d.created_at.strftime('%Y-%m-%d')}_Новый диалог" if d.created_at else "Новый диалог"
                    s.commit()
                    s.refresh(d)

                u.active_dialog_id = d.id
                s.commit()
                s.refresh(u)

            return u

    def get_user(self, tg_id: str) -> Optional[User]:
        with self.sf() as s:
            return (
                s.execute(select(User).where(User.tg_id == str(tg_id)))
                .scalars()
                .first()
            )

    def set_active_dialog(self, user_id: int, dialog_id: int) -> None:
        with self.sf() as s:
            u = s.get(User, user_id)
            if not u:
                return
            u.active_dialog_id = int(dialog_id)
            s.commit()

    # ---------- dialogs ----------
    def get_active_dialog(self, user_id: int) -> Optional[Dialog]:
        with self.sf() as s:
            u = s.get(User, user_id)
            if not u or not u.active_dialog_id:
                return None
            return (
                s.execute(
                    select(Dialog).where(
                        Dialog.id == int(u.active_dialog_id),
                        Dialog.user_id == int(user_id),
                    )
                )
                .scalars()
                .first()
            )

    def get_dialog_for_user(self, dialog_id: int, user_id: int) -> Optional[Dialog]:
        with self.sf() as s:
            return (
                s.execute(
                    select(Dialog).where(
                        Dialog.id == int(dialog_id),
                        Dialog.user_id == int(user_id),
                    )
                )
                .scalars()
                .first()
            )

    def new_dialog(self, user_id: int, title: str = "", settings: Optional[Dict[str, Any]] = None) -> Dialog:
        with self.sf() as s:
            d = Dialog(user_id=int(user_id), title=(title or "").strip(), settings=settings or {})
            s.add(d)
            s.commit()
            s.refresh(d)

            if not (d.title or "").strip():
                d.title = f"{d.created_at.strftime('%Y-%m-%d')}_Новый диалог" if d.created_at else "Новый диалог"

            d.updated_at = func.now()
            s.commit()
            s.refresh(d)
            return d

    def rename_dialog(self, dialog_id: int, title: str) -> Optional[Dialog]:
        with self.sf() as s:
            d = s.get(Dialog, int(dialog_id))
            if not d:
                return None
            d.title = (title or "").strip()
            d.updated_at = func.now()
            s.commit()
            s.refresh(d)
            return d

    def update_dialog_settings(self, dialog_id: int, patch: Dict[str, Any]) -> Optional[Dialog]:
        """Обновить settings диалога (JSON) merge-патчем."""
        patch = patch or {}
        with self.sf() as s:
            d = s.get(Dialog, int(dialog_id))
            if not d:
                return None

            cur = d.settings if isinstance(d.settings, dict) else {}
            merged = dict(cur)
            merged.update(dict(patch))
            d.settings = merged
            d.updated_at = func.now()

            s.commit()
            s.refresh(d)
            return d

    def delete_dialog(self, dialog_id: int) -> None:
        with self.sf() as s:
            d = s.get(Dialog, int(dialog_id))
            if not d:
                return
            s.delete(d)
            s.commit()

    def list_dialogs(self, user_id: int, limit: int = 20) -> List[Dialog]:
        with self.sf() as s:
            q = (
                select(Dialog)
                .where(Dialog.user_id == int(user_id))
                .order_by(nullslast(desc(Dialog.updated_at)), desc(Dialog.id))
                .limit(int(limit))
            )
            return list(s.execute(q).scalars().all())

    def list_dialogs_page(self, user_id: int, limit: int, offset: int) -> List[Dialog]:
        with self.sf() as s:
            q = (
                select(Dialog)
                .where(Dialog.user_id == int(user_id))
                .order_by(nullslast(desc(Dialog.updated_at)), desc(Dialog.id))
                .limit(int(limit))
                .offset(int(offset))
            )
            return list(s.execute(q).scalars().all())

    def count_dialogs(self, user_id: int) -> int:
        with self.sf() as s:
            q = select(func.count(Dialog.id)).where(Dialog.user_id == int(user_id))
            return int(s.execute(q).scalar() or 0)

    # ---------- messages ----------
    def add_message(self, dialog_id: int, role: str, content: str) -> Message:
        with self.sf() as s:
            m = Message(dialog_id=int(dialog_id), role=str(role), content=str(content))
            s.add(m)

            d = s.get(Dialog, int(dialog_id))
            if d:
                d.updated_at = func.now()

            s.commit()
            s.refresh(m)
            return m

    def list_messages(self, dialog_id: int, limit: int = 30) -> List[Message]:
        with self.sf() as s:
            q = (
                select(Message)
                .where(Message.dialog_id == int(dialog_id))
                .order_by(Message.id.desc())
                .limit(int(limit))
            )
            rows = list(s.execute(q).scalars().all())
            rows.reverse()
            return rows
