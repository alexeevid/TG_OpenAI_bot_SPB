from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text, func
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Dialog(Base):
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String(128), nullable=False, default="Диалог")
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    messages = relationship("Message", back_populates="dialog", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dialog_id = Column(Integer, ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # "user", "assistant", "system"
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now())

    dialog = relationship("Dialog", back_populates="messages")
