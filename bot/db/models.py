
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, Text, ForeignKey, DateTime, Float
from sqlalchemy.dialects.postgresql import ARRAY
import datetime

Base = declarative_base()

class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    path = Column(Text, nullable=False, unique=True)
    size = Column(Integer, nullable=False)
    sha256 = Column(Text, nullable=False)
    password_required = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    embedding = Column(ARRAY(Float), nullable=False)

    document = relationship("Document", back_populates="chunks")
