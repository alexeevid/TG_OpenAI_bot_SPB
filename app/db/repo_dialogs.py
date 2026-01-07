# app/db/repo_dialogs.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, nullslast, select
from sqlalchemy.orm import Session

from .models import Dialog, Message, User


__all__ = ["DialogsRepo"]


def _today_prefix(dt: Optional[datetime]) -> str:
    if not dt:
        return datetime.utcnow().strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _masked_title(created_at: Optional[datetime], name: str) -> str:
    n = (name or "").strip()
    if not n:
        n = "Новый диалог"
    return f"{_today_prefix(created_at)}_{n}"


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

                    d.title = _masked_title(getattr(d, "created_at", None), "Новый диалог")
                    d.updated_at = getattr(d, "created_at", None) or func.now()
                    s.commit()
                    s.refresh(d)

                u.active_dialog_id = d.id
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
            d = Dialog(user_id=user_id, title=(title or "").strip(), settings=settings or {})
            s.add(d)
            s.commit()
            s.refresh(d)

            if not (d.title or "").strip():
                d.title = _masked_title(getattr(d, "created_at", None), "Новый диалог")
            d.updated_at = getattr(d, "created_at", None) or func.now()
            s.commit()
            s.refresh(d)
            return d

    def count_dialogs(self, user_id: int) -> int:
        with self.sf() as s:
            q = select(func.count(Dialog.id)).where(Dialog.user_id == user_id)
            return int(s.execute(q).scalar() or 0)

    def list_dialogs_page(self, user_id: int, limit: int, offset: int) -> List[Dialog]:
        with self.sf() as s:
            q = (
                select(Dialog)
                .where(Dialog.user_id == user_id)
                .order_by(nullslast(desc(Dialog.updated_at)), desc(Dialog.id))
                .limit(limit)
                .offset(offset)
            )
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

            if not patch or not isinstance(patch, dict):
                return d

            base = d.settings or {}
            if not isinstance(base, dict):
                base = {}
            else:
                base = dict(base)

            clean_patch: Dict[str, Any] = {k: v for k, v in (patch or {}).items() if v is not None}
            if not clean_patch:
                return d

            base.update(clean_patch)
            d.settings = base
            d.updated_at = func.now()

            s.commit()
            s.refresh(d)
            return d

    def rename_dialog(self, dialog_id: int, title: str) -> Optional[Dialog]:
        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return None
            d.title = (title or "").strip()
            d.updated_at = func.now()
            s.commit()
            s.refresh(d)
            return d

    def delete_dialog(self, dialog_id: int) -> None:
        with self.sf() as s:
            d = s.get(Dialog, dialog_id)
            if not d:
                return

            u = s.get(User, d.user_id)
            s.delete(d)
            s.commit()

            if u and u.active_dialog_id == dialog_id:
                next_d = (
                    s.execute(
                        select(Dialog)
                        .where(Dialog.user_id == u.id)
                        .order_by(nullslast(desc(Dialog.updated_at)), desc(Dialog.id))
                    )
                    .scalars()
                    .first()
                )
                u.active_dialog_id = next_d.id if next_d else None
                s.commit()

    # ---------- messages ----------
    def add_message(self, dialog_id: int, role: str, content: str) -> Message:
        with self.sf() as s:
            m = Message(dialog_id=dialog_id, role=role, content=content)
            s.add(m)

            d = s.get(Dialog, dialog_id)
            if d:
                d.updated_at = func.now()

            s.commit()
            s.refresh(m)
            return m

    def list_messages(self, dialog_id: int, limit: int = 30) -> List[Message]:
        with self.sf() as s:
            q = (
                select(Message)
                .where(Message.dialog_id == dialog_id)
                .order_by(Message.id.desc())
                .limit(limit)
            )
            return list(reversed(list(s.execute(q).scalars().all())))
