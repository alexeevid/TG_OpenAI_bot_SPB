
from sqlalchemy.orm import Session
from .models import User, Dialog, Message

class DialogsRepo:
    def __init__(self, sf):
        self.sf = sf

    def ensure_user(self, tg_id: str):
        with self.sf() as s:  # type: Session
            u = s.query(User).filter_by(tg_id=tg_id).first()
            if not u:
                u = User(tg_id=tg_id)
                s.add(u); s.commit(); s.refresh(u)
            return u

    def new_dialog(self, user_id: int, title: str=""):
        with self.sf() as s:
            d = Dialog(user_id=user_id, title=title)
            s.add(d); s.commit(); s.refresh(d)
            return d

    def add_message(self, dialog_id: int, role: str, content: str):
        with self.sf() as s:
            m = Message(dialog_id=dialog_id, role=role, content=content)
            s.add(m); s.commit(); s.refresh(m)
            return m
