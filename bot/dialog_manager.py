from datetime import datetime
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc
from .db.models import Dialog, Message
from .db.session import SessionLocal


class DialogManager:
    """
    Класс для работы с диалогами и сообщениями через PostgreSQL.
    """

    def __init__(self):
        pass

    def create_dialog(self, user_id: int, title: Optional[str] = None,
                      model: Optional[str] = None, style: Optional[str] = None,
                      kb_docs: Optional[list] = None) -> Dialog:
        """
        Создание нового диалога.
        """
        with SessionLocal() as db:
            dialog = Dialog(
                user_id=user_id,
                title=title or "Диалог",
                created_at=datetime.utcnow(),
                last_message_at=datetime.utcnow(),
                is_deleted=False,
                model=model,
                style=style,
                kb_documents=kb_docs or []
            )
            db.add(dialog)
            db.commit()
            db.refresh(dialog)
            return dialog

    def get_active_dialogs(self, user_id: int) -> List[Dialog]:
        """
        Получение всех активных (не удалённых) диалогов пользователя.
        """
        with SessionLocal() as db:
            return (
                db.query(Dialog)
                .filter(Dialog.user_id == user_id, Dialog.is_deleted == False)
                .order_by(desc(Dialog.last_message_at))
                .all()
            )

    def get_dialog_state(self, dialog_id, user_id):
        """Возвращает объект диалога с настройками."""
        return self.get_dialog(dialog_id, user_id)
    
    def save_dialog_state(self, dialog_id, user_id, dlg_state):
        """Сохраняет изменения состояния диалога в БД."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if dialog:
                dialog.model = getattr(dlg_state, "model", dialog.model)
                dialog.style = getattr(dlg_state, "style", dialog.style)
                dialog.kb_documents = getattr(dlg_state, "kb_documents", dialog.kb_documents)
                session.commit()
    
    def get_active_dialog(self, user_id: int) -> Optional[Dialog]:
        """
        Получить последний активный диалог пользователя.
        """
        with SessionLocal() as db:
            return (
                db.query(Dialog)
                .filter(Dialog.user_id == user_id, Dialog.is_deleted == False)
                .order_by(desc(Dialog.last_message_at))
                .first()
            )

    def get_dialog(self, dialog_id: int, user_id: int) -> Optional[Dialog]:
        """
        Получение диалога по ID (с проверкой пользователя).
        """
        with SessionLocal() as db:
            return (
                db.query(Dialog)
                .filter(Dialog.id == dialog_id, Dialog.user_id == user_id)
                .first()
            )

    def rename_dialog(self, dialog_id: int, user_id: int, new_title: str) -> bool:
        """
        Переименование диалога.
        """
        with SessionLocal() as db:
            dialog = (
                db.query(Dialog)
                .filter(Dialog.id == dialog_id, Dialog.user_id == user_id)
                .first()
            )
            if dialog:
                dialog.title = new_title
                db.commit()
                return True
            return False

    def get_dialog_state(self, dialog_id, user_id):
        """Возвращает объект диалога с текущими настройками модели и стиля."""
        dlg = self.get_dialog(dialog_id, user_id)
        if dlg is None:
            return None
        return dlg
    
    def soft_delete_dialog(self, dialog_id: int, user_id: int) -> bool:
        """
        Мягкое удаление диалога.
        """
        with SessionLocal() as db:
            dialog = (
                db.query(Dialog)
                .filter(Dialog.id == dialog_id, Dialog.user_id == user_id)
                .first()
            )
            if dialog:
                dialog.is_deleted = True
                db.commit()
                return True
            return False

    def export_dialog(self, dialog_id: int, user_id: int) -> Optional[str]:
        """
        Экспорт диалога в формате Markdown.
        """
        with SessionLocal() as db:
            messages = (
                db.query(Message)
                .filter(Message.dialog_id == dialog_id)
                .order_by(Message.timestamp)
                .all()
            )
            if not messages:
                return None
            md_lines = []
            for m in messages:
                role_prefix = "**Пользователь:**" if m.role == "user" else "**Ассистент:**"
                md_lines.append(f"{role_prefix} {m.content}")
            return "\n\n".join(md_lines)

    def add_message(self, dialog_id: int, role: str, text: str) -> None:
        """
        Добавить сообщение в диалог.
        """
        with SessionLocal() as db:
            message = Message(
                dialog_id=dialog_id,
                role=role,
                content=text,
                timestamp=datetime.utcnow()
            )
            db.add(message)
            dialog = db.query(Dialog).filter(Dialog.id == dialog_id).first()
            if dialog:
                dialog.last_message_at = datetime.utcnow()
            db.commit()

    def reset_user_dialogs(self, user_id):
        """Удаляет все диалоги и состояния для пользователя."""
        self._dialogs_by_user.pop(user_id, None)
        self._current_dialog_by_user.pop(user_id, None)

    
    def get_messages(self, dialog_id: int, limit: int = 10) -> List[Message]:
        """
        Получить последние N сообщений диалога.
        """
        with SessionLocal() as db:
            return (
                db.query(Message)
                .filter(Message.dialog_id == dialog_id)
                .order_by(desc(Message.timestamp))
                .limit(limit)
                .all()
            )

    def update_model(self, dialog_id: int, user_id: int, model: str) -> bool:
        """
        Обновить модель, закреплённую за диалогом.
        """
        with SessionLocal() as db:
            dialog = self.get_dialog(dialog_id, user_id)
            if dialog:
                dialog.model = model
                db.commit()
                return True
            return False

    def update_style(self, dialog_id: int, user_id: int, style: str) -> bool:
        """
        Обновить стиль (роль) для диалога.
        """
        with SessionLocal() as db:
            dialog = self.get_dialog(dialog_id, user_id)
            if dialog:
                dialog.style = style
                db.commit()
                return True
            return False
