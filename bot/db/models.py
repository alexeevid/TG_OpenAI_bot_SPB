from datetime import datetime
from typing import Optional
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime, Boolean

Base = declarative_base()

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # 👇 Новое поле — маппинг на существующую колонку в БД
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    mime: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(256), default="Диалог", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
