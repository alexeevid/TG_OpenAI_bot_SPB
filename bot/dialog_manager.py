from datetime import datetime
from typing import List, Optional
from sqlalchemy import desc
from .db.models import Dialog, Message
from .db.session import SessionLocal


class DialogManager:
    """
    Класс для работы с диалогами и сообщениями через PostgreSQL.
    """

    def __init__(self):
        # Единая точка входа для сессий SQLAlchemy
        self.Session = SessionLocal

    # ───── Работа с состоянием диалога ─────
    def get_dialog_state(self, dialog_id: int, user_id: int) -> Optional[Dialog]:
        """Возвращает объект диалога с настройками."""
        with self.Session() as session:
            return session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()

    def save_dialog_state(self, dialog_id: int, user_id: int, dlg_state: Dialog) -> bool:
        """Сохраняет изменения состояния диалога в БД."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if not dialog:
                return False
            dialog.model = getattr(dlg_state, "model", dialog.model)
            dialog.style = getattr(dlg_state, "style", dialog.style)
            dialog.kb_documents = getattr(dlg_state, "kb_documents", dialog.kb_documents)
            session.commit()
            return True

    # ───── CRUD для диалогов ─────
    def create_dialog(
        self,
        user_id: int,
        title: Optional[str] = None,
        model: Optional[str] = None,
        style: Optional[str] = None,
        kb_docs: Optional[list] = None
    ) -> Dialog:
        """Создаёт новый диалог."""
        with self.Session() as session:
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
            session.add(dialog)
            session.commit()
            session.refresh(dialog)
            return dialog

    def get_active_dialogs(self, user_id: int) -> List[Dialog]:
        """Возвращает все активные диалоги пользователя."""
        with self.Session() as session:
            return (
                session.query(Dialog)
                .filter(Dialog.user_id == user_id, Dialog.is_deleted == False)
                .order_by(desc(Dialog.last_message_at))
                .all()
            )

    def get_active_dialog(self, user_id: int) -> Optional[Dialog]:
        """Возвращает последний активный диалог пользователя."""
        with self.Session() as session:
            return (
                session.query(Dialog)
                .filter(Dialog.user_id == user_id, Dialog.is_deleted == False)
                .order_by(desc(Dialog.last_message_at))
                .first()
            )

    def get_dialog(self, dialog_id: int, user_id: int) -> Optional[Dialog]:
        """Возвращает диалог по ID."""
        with self.Session() as session:
            return (
                session.query(Dialog)
                .filter(Dialog.id == dialog_id, Dialog.user_id == user_id)
                .first()
            )

    def rename_dialog(self, dialog_id: int, user_id: int, new_title: str) -> bool:
        """Переименовывает диалог."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if not dialog:
                return False
            dialog.title = new_title
            session.commit()
            return True

    def soft_delete_dialog(self, dialog_id: int, user_id: int) -> bool:
        """Мягкое удаление диалога."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if not dialog:
                return False
            dialog.is_deleted = True
            session.commit()
            return True

    # ───── Работа с сообщениями ─────
    def add_message(self, dialog_id: int, role: str, text: str) -> None:
        """Добавляет сообщение в диалог."""
        with self.Session() as session:
            message = Message(
                dialog_id=dialog_id,
                role=role,
                content=text,
                timestamp=datetime.utcnow()
            )
            session.add(message)
            dialog = session.query(Dialog).filter(Dialog.id == dialog_id).first()
            if dialog:
                dialog.last_message_at = datetime.utcnow()
            session.commit()

    def get_messages(self, dialog_id: int, limit: int = 10) -> List[Message]:
        """Возвращает последние N сообщений диалога."""
        with self.Session() as session:
            return (
                session.query(Message)
                .filter(Message.dialog_id == dialog_id)
                .order_by(desc(Message.timestamp))
                .limit(limit)
                .all()
            )

    def export_dialog(self, dialog_id: int, user_id: int) -> Optional[str]:
        """Экспортирует диалог в формате Markdown."""
        with self.Session() as session:
            messages = (
                session.query(Message)
                .filter(Message.dialog_id == dialog_id)
                .order_by(Message.timestamp)
                .all()
            )
            if not messages:
                return None
            lines = []
            for m in messages:
                prefix = "**Пользователь:**" if m.role == "user" else "**Ассистент:**"
                lines.append(f"{prefix} {m.content}")
            return "\n\n".join(lines)

    # ───── Обновление параметров диалога ─────
    def update_model(self, dialog_id: int, user_id: int, model: str) -> bool:
        """Обновляет модель для диалога."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if not dialog:
                return False
            dialog.model = model
            session.commit()
            return True

    def update_style(self, dialog_id: int, user_id: int, style: str) -> bool:
        """Обновляет стиль общения для диалога."""
        with self.Session() as session:
            dialog = session.query(Dialog).filter_by(id=dialog_id, user_id=user_id).first()
            if not dialog:
                return False
            dialog.style = style
            session.commit()
            return True
