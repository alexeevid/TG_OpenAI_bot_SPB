from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Text,
    DateTime,
    func,
    JSON,
    Boolean,
    BigInteger,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from pgvector.sqlalchemy import Vector

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

    # Path на Я.Диске (или ваш стабильный идентификатор документа)
    path = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=True)

    # Для синхронизации с Я.Диском
    resource_id = Column(String, unique=True, nullable=True, index=True)
    md5 = Column(String, nullable=True)
    size = Column(BigInteger, nullable=True)
    modified_at = Column(DateTime, nullable=True)

    # Управление жизненным циклом
    is_active = Column(Boolean, nullable=False, default=True)

    # Статус индексации (для надёжного KB sync)
    status = Column(String, nullable=False, default="new")  # new|indexed|error|skipped|inactive
    indexed_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True)

    # Порядок чанка в документе для воспроизводимых цитат
    chunk_order = Column(Integer, nullable=False, default=0)

    text = Column(Text, nullable=False)

    # pgvector: хранение эмбеддинга как VECTOR(dim)
    embedding = Column(Vector(3072), nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    document = relationship("KBDocument")


class DialogKBDocument(Base):
    """
    Связь диалог ↔ документ БЗ (many-to-many через таблицу).
    is_enabled позволяет быстро исключать документ из контекста, не удаляя связь.
    """

    __tablename__ = "dialog_kb_documents"

    # Ключевой момент: ON CONFLICT (dialog_id, document_id) требует UNIQUE/PK на эти поля.
    __table_args__ = (
        UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_documents_dialog_doc"),
        Index("ix_dialog_kb_documents_dialog_id", "dialog_id"),
        Index("ix_dialog_kb_documents_document_id", "document_id"),
    )

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

    __table_args__ = (
        UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_secrets_dialog_doc"),
        Index("ix_dialog_kb_secrets_dialog_id", "dialog_id"),
        Index("ix_dialog_kb_secrets_document_id", "document_id"),
    )

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False)

    pdf_password = Column(Text, nullable=False)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="kb_secrets")
    document = relationship("KBDocument")
