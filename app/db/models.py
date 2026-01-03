from __future__ import annotations

from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, func, JSON, Boolean, BigInteger
from sqlalchemy.orm import relationship

from .session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, default="user", nullable=False)

    active_dialog_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    dialogs = relationship("Dialog", back_populates="user", foreign_keys="Dialog.user_id")


class Dialog(Base):
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    title = Column(String, default="", nullable=False)

    # Пер-диалог настройки: выбранная модель, режим, kb_mode, и т.п.
    settings = Column(JSON, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="dialogs", foreign_keys=[user_id])
    messages = relationship("Message", back_populates="dialog", cascade="all, delete-orphan")

    kb_documents = relationship("DialogKBDocument", back_populates="dialog", cascade="all, delete-orphan")
    kb_secrets = relationship("DialogKBSecret", back_populates="dialog", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=False)

    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="messages")


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id = Column(Integer, primary_key=True)

    # Path на Я.Диске (или локальный идентификатор в вашей логике)
    path = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=True)

    # Для синка с Я.Диском (best practice)
    resource_id = Column(String, unique=True, nullable=True)
    md5 = Column(String, nullable=True)
    size = Column(BigInteger, nullable=True)
    modified_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)

    # Сериализованный список float (JSON-строка). (Если у вас уже pgvector-тип — замените позже централизованно.)
    embedding = Column(Text, nullable=False)

    document = relationship("KBDocument")


class DialogKBDocument(Base):
    """
    Связь диалог ↔ документ БЗ (many-to-many через таблицу).
    is_enabled позволяет быстро исключать документ из контекста, не удаляя связь.
    """

    __tablename__ = "dialog_kb_documents"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)

    is_enabled = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="kb_documents")
    document = relationship("KBDocument")


class DialogKBSecret(Base):
    """
    Пароли PDF строго в рамках диалога.
    """

    __tablename__ = "dialog_kb_secrets"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)

    pdf_password = Column(Text, nullable=False)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="kb_secrets")
    document = relationship("KBDocument")
