from __future__ import annotations

from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, func, JSON, BigInteger
from sqlalchemy.orm import relationship

from .session import Base

try:
    from pgvector.sqlalchemy import Vector
except Exception:
    Vector = None


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True, index=True, nullable=False)
    role = Column(String, default="user", nullable=False)

    active_dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    dialogs = relationship("Dialog", back_populates="user", foreign_keys="Dialog.user_id")
    active_dialog = relationship("Dialog", foreign_keys=[active_dialog_id], post_update=True)


class Dialog(Base):
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    title = Column(String, default="", nullable=False)
    settings = Column(JSON, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="dialogs", foreign_keys=[user_id])
    messages = relationship("Message", back_populates="dialog", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id"), nullable=False)

    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    dialog = relationship("Dialog", back_populates="messages")


class KBFile(Base):
    __tablename__ = "kb_files"

    resource_id = Column(String(128), primary_key=True)
    path = Column(Text, nullable=False)

    modified_disk = Column(DateTime(timezone=False), nullable=True)
    md5_disk = Column(String(64), nullable=True)
    size_disk = Column(BigInteger(), nullable=True)

    indexed_at = Column(DateTime(timezone=False), nullable=True)
    status = Column(String(32), nullable=False, default="new")
    last_error = Column(Text, nullable=True)
    last_checked_at = Column(DateTime(timezone=False), nullable=True)


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id = Column(Integer, primary_key=True)
    resource_id = Column(String(128), unique=True, nullable=False)
    path = Column(Text, nullable=False)
    title = Column(String, nullable=True)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    chunks = relationship("KBChunk", back_populates="document", cascade="all,delete-orphan")


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_order = Column(Integer, nullable=False, default=0)
    text = Column(Text, nullable=False)

    if Vector is None:
        embedding = Column(Text, nullable=True)
    else:
        embedding = Column(Vector(3072), nullable=True)

    document = relationship("KBDocument", back_populates="chunks")
