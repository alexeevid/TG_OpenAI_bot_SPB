
from sqlalchemy import Column, Integer, String, ForeignKey, Text, DateTime, func, Float
from sqlalchemy.orm import relationship
from .session import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(String, unique=True, index=True)
    role = Column(String, default="user")
    created_at = Column(DateTime, server_default=func.now())

class Dialog(Base):
    __tablename__ = "dialogs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String, default="")
    created_at = Column(DateTime, server_default=func.now())
    user = relationship("User")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id"))
    role = Column(String)
    content = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

class KBDocument(Base):
    __tablename__ = "kb_documents"
    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True)
    title = Column(String, nullable=True)
    updated_at = Column(DateTime, server_default=func.now())

class KBChunk(Base):
    __tablename__ = "kb_chunks"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("kb_documents.id"))
    text = Column(Text, nullable=False)
    # Храним эмбеддинг как массив float ради простоты миграции; можно переключить на pgvector тип и индекс ivfflat
    embedding = Column(Text, nullable=False)  # сериализованный список float в JSON-строку
    document = relationship("KBDocument")
