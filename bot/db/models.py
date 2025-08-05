from sqlalchemy import Column, Integer, String, Text, BigInteger, TIMESTAMP
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Dialog(Base):
    __tablename__ = "dialogs"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    dialog_id = Column(String(64), nullable=False)
    created_at = Column(TIMESTAMP, nullable=False)
    documents = Column(Text)  # храним список id документов в json или str
    status = Column(String(32), default="active")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    dialog_id = Column(String(64), nullable=False)
    message_id = Column(String(64))
    role = Column(String(16))
    text = Column(Text)
    kb_chunks = Column(Text)  # список id чанков через запятую или json
    timestamp = Column(TIMESTAMP, nullable=False)
