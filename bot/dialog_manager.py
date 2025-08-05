from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional, Tuple
from bot.db.session import SessionLocal
from bot.db.models import Dialog, Message

class DialogManager:
    def __init__(self):
        # Можно добавить кеш при необходимости
        pass

    def create_dialog(self, user_id: int, title: Optional[str] = None) -> Dialog:
        with SessionLocal() as db:
            if not title:
                title = "Диалог"
            dlg = Dialog(user_id=user_id, title=title)
            db.add(dlg)
            db.commit()
            db.refresh(dlg)
            return dlg

    def get_active_dialogs(self, user_id: int) -> List[Dialog]:
        with SessionLocal() as db:
            return db.query(Dialog).filter_by(user_id=user_id, is_deleted=False).order_by(Dialog.updated_at.desc()).all()

    def get_dialog(self, dialog_id: int, user_id: Optional[int] = None) -> Optional[Dialog]:
        with SessionLocal() as db:
            q = db.query(Dialog).filter_by(id=dialog_id)
            if user_id is not None:
                q = q.filter_by(user_id=user_id)
            return q.first()

    def soft_delete_dialog(self, dialog_id: int, user_id: Optional[int] = None) -> bool:
        with SessionLocal() as db:
            q = db.query(Dialog).filter_by(id=dialog_id)
            if user_id is not None:
                q = q.filter_by(user_id=user_id)
            dlg = q.first()
            if dlg:
                dlg.is_deleted = True
                db.commit()
                return True
            return False

    def rename_dialog(self, dialog_id: int, new_title: str, user_id: Optional[int] = None) -> bool:
        with SessionLocal() as db:
            q = db.query(Dialog).filter_by(id=dialog_id)
            if user_id is not None:
                q = q.filter_by(user_id=user_id)
            dlg = q.first()
            if dlg:
                dlg.title = new_title
                dlg.updated_at = datetime.now()
                db.commit()
                return True
            return False

    def add_message(self, dialog_id: int, role: str, text: str) -> Optional[Message]:
        with SessionLocal() as db:
            dlg = db.query(Dialog).filter_by(id=dialog_id, is_deleted=False).first()
            if not dlg:
                return None
            msg = Message(dialog_id=dialog_id, role=role, text=text)
            dlg.updated_at = datetime.now()
            db.add(msg)
            db.commit()
            db.refresh(msg)
            return msg

    def get_messages(self, dialog_id: int, limit: Optional[int] = None) -> List[Message]:
        with SessionLocal() as db:
            q = db.query(Message).filter_by(dialog_id=dialog_id).order_by(Message.created_at.asc())
            if limit:
                q = q.limit(limit)
            return q.all()

    def get_last_message(self, dialog_id: int) -> Optional[Message]:
        with SessionLocal() as db:
            return db.query(Message).filter_by(dialog_id=dialog_id).order_by(Message.created_at.desc()).first()

    def generate_summary(self, dialog_id: int) -> str:
        """Генерация краткого заголовка диалога (по первым 1-2 сообщениям пользователя)."""
        with SessionLocal() as db:
            msgs = db.query(Message).filter_by(dialog_id=dialog_id).order_by(Message.created_at.asc()).limit(2).all()
            summary = ""
            for msg in msgs:
                if msg.role == "user":
                    summary += (msg.text[:50] + ("…" if len(msg.text) > 50 else "")) + " "
            summary = summary.strip() or "Диалог"
            return summary[:64]  # Ограничим длину

    def update_title_by_summary(self, dialog_id: int) -> None:
        """Обновить заголовок по автосгенерированному summary."""
        summary = self.generate_summary(dialog_id)
        self.rename_dialog(dialog_id, summary)

    def export_dialog(self, dialog_id: int) -> str:
        """Выгрузить диалог в формате Markdown."""
        with SessionLocal() as db:
            dlg = db.query(Dialog).filter_by(id=dialog_id).first()
            if not dlg:
                return "Диалог не найден."
            msgs = db.query(Message).filter_by(dialog_id=dialog_id).order_by(Message.created_at.asc()).all()
            lines = [f"# Диалог: {dlg.title}\nДата создания: {dlg.created_at.strftime('%Y.%m.%d %H:%M')}\n"]
            for msg
