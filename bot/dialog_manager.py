from bot.db.session import SessionLocal
from bot.db.models import Dialog, Message
from datetime import datetime

class DialogManager:
    def __init__(self):
        self.db = SessionLocal()

    def create_dialog(self, user_id, dialog_id, documents=None):
        dlg = Dialog(
            user_id=user_id,
            dialog_id=dialog_id,
            created_at=datetime.utcnow(),
            documents=documents or "",
            status="active"
        )
        self.db.add(dlg)
        self.db.commit()
        return dlg

    def add_message(self, dialog_id, message_id, role, text, kb_chunks=""):
        msg = Message(
            dialog_id=dialog_id,
            message_id=message_id,
            role=role,
            text=text,
            kb_chunks=kb_chunks,
            timestamp=datetime.utcnow(),
        )
        self.db.add(msg)
        self.db.commit()
        return msg

    def get_active_dialog(self, user_id):
        dlg = self.db.query(Dialog).filter_by(user_id=user_id, status="active").order_by(Dialog.created_at.desc()).first()
        return dlg

    def get_dialog_messages(self, dialog_id):
        msgs = self.db.query(Message).filter_by(dialog_id=dialog_id).order_by(Message.timestamp.asc()).all()
        return msgs

    def set_documents(self, dialog_id, documents):
        dlg = self.db.query(Dialog).filter_by(dialog_id=dialog_id).first()
        if dlg:
            dlg.documents = documents
            self.db.commit()
        return dlg

    def get_documents(self, dialog_id):
        dlg = self.db.query(Dialog).filter_by(dialog_id=dialog_id).first()
        return dlg.documents if dlg else ""
