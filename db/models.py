from __future__ import annotations

from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, func, JSON
from sqlalchemy.orm import relationship

from .session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, default="user", nullable=False)

    # Текущий активный диалог пользователя (для удобства в Telegram).
    active_dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    dialogs = relationship("Dialog", back_populates="user", foreign_keys="Dialog.user_id")
    active_dialog = relationship("Dialog", foreign_keys=[active_dialog_id], post_update=True)


class Dialog(Base):
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    title = Column(String, default="", nullable=False)

    # Пер-диалог настройки: выбранная модель, режим, включение KB и т.п.
    settings = Column(JSON, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="dialogs", foreign_keys=[user_id])
    messages = relationship("Message", back_populates="dialog", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=False)

    role = Column(String, nullable=False)   # "user" | "assistant" | "system"
    content = Column(Text, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="messages")


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kb_documents.id"), nullable=False)
    text = Column(Text, nullable=False)

    # Сериализованный список float (JSON-строка). Можно заменить на pgvector позже.
    embedding = Column(Text, nullable=False)

    document = relationship("KBDocument")
