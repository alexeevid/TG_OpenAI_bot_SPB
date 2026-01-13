from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import AccessEntry


@dataclass
class AccessRow:
    tg_id: str
    is_allowed: bool
    is_admin: bool
    note: str = ""


class AccessRepo:
    def __init__(self, sf):
        self.sf = sf

    def has_any_entries(self) -> bool:
        with self.sf() as s:  # type: Session
            cnt = s.execute(select(func.count(AccessEntry.id))).scalar_one()
            return int(cnt or 0) > 0

    def get(self, tg_id: int) -> Optional[AccessEntry]:
        tid = str(tg_id)
        with self.sf() as s:  # type: Session
            return (
                s.execute(select(AccessEntry).where(AccessEntry.tg_id == tid))
                .scalars()
                .first()
            )

    def list(self, limit: int = 200) -> List[AccessRow]:
        with self.sf() as s:  # type: Session
            q = select(AccessEntry).order_by(AccessEntry.id.asc()).limit(int(limit))
            rows = s.execute(q).scalars().all()

            out: List[AccessRow] = []
            for r in rows:
                out.append(
                    AccessRow(
                        tg_id=r.tg_id,
                        is_allowed=bool(r.is_allowed),
                        is_admin=bool(r.is_admin),
                        note=r.note or "",
                    )
                )
            return out


    def upsert(self, tg_id: int, *, allow: bool, admin: bool = False, note: str = "") -> AccessEntry:
        tid = str(tg_id)
        with self.sf() as s:  # type: Session
            obj = (
                s.execute(select(AccessEntry).where(AccessEntry.tg_id == tid))
                .scalars()
                .first()
            )
            if not obj:
                obj = AccessEntry(tg_id=tid)
                s.add(obj)

            obj.is_allowed = bool(allow)
            obj.is_admin = bool(admin)
            obj.note = note or ""

            s.commit()
            s.refresh(obj)
            return obj

    def set_admin(self, tg_id: int, *, is_admin: bool, note: str = "") -> AccessEntry:
        tid = str(tg_id)
        with self.sf() as s:  # type: Session
            obj = (
                s.execute(select(AccessEntry).where(AccessEntry.tg_id == tid))
                .scalars()
                .first()
            )
            if not obj:
                obj = AccessEntry(tg_id=tid, is_allowed=True)
                s.add(obj)

            obj.is_admin = bool(is_admin)
            if note:
                obj.note = note

            # Админ по смыслу должен быть allowed
            if obj.is_admin:
                obj.is_allowed = True

            s.commit()
            s.refresh(obj)
            return obj

    def delete(self, tg_id: int) -> bool:
        tid = str(tg_id)
        with self.sf() as s:  # type: Session
            obj = (
                s.execute(select(AccessEntry).where(AccessEntry.tg_id == tid))
                .scalars()
                .first()
            )
            if not obj:
                return False
            s.delete(obj)
            s.commit()
            return True
