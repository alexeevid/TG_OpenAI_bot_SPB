from sqlalchemy import Column, BigInteger, Text, Boolean, Integer, DateTime, ForeignKey, JSON, String
from pgvector.sqlalchemy import Vector
from bot.db.base import Base
class User(Base):
    __tablename__='users'
    id = Column(BigInteger, primary_key=True)
    tg_user_id = Column(BigInteger, unique=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_allowed = Column(Boolean, default=True, nullable=False)
    lang = Column(String(10), default='ru', nullable=False)
    created_at = Column(DateTime(timezone=True))
class Dialog(Base):
    __tablename__='dialogs'
    id = Column(BigInteger, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = Column(Text)
    style = Column(String(20), default='expert', nullable=False)
    model = Column(Text)
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True))
    last_message_at = Column(DateTime(timezone=True))
class Message(Base):
    __tablename__='messages'
    id = Column(BigInteger, primary_key=True)
    dialog_id = Column(BigInteger, ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    tokens = Column(Integer)
    created_at = Column(DateTime(timezone=True))
class KbDocument(Base):
    __tablename__='kb_documents'
    id = Column(BigInteger, primary_key=True)
    path = Column(Text, unique=True, nullable=False)
    etag = Column(Text)
    mime = Column(Text)
    pages = Column(Integer)
    bytes = Column(BigInteger)
    updated_at = Column(DateTime(timezone=True))
    is_active = Column(Boolean, default=True, nullable=False)
class KbChunk(Base):
    __tablename__='kb_chunks'
    id = Column(BigInteger, primary_key=True)
    document_id = Column(BigInteger, ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    meta = Column(JSON)
    embedding = Column(Vector(dim=3072))
class DialogKbLink(Base):
    __tablename__='dialog_kb_links'
    id = Column(BigInteger, primary_key=True)
    dialog_id = Column(BigInteger, ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False)
    document_id = Column(BigInteger, ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime(timezone=True))
class PdfPassword(Base):
    __tablename__='pdf_passwords'
    id = Column(BigInteger, primary_key=True)
    dialog_id = Column(BigInteger, ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False)
    document_id = Column(BigInteger, ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False)
    pwd_hash = Column(Text)
    created_at = Column(DateTime(timezone=True))
class AuditLog(Base):
    __tablename__='audit_log'
    id = Column(BigInteger, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.id', ondelete='SET NULL'))
    event = Column(Text, nullable=False)
    payload = Column(JSON)
    created_at = Column(DateTime(timezone=True))
