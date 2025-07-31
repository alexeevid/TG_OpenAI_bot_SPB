# bot/db/session.py
from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from bot.settings import Settings
from bot.db.utils import normalize_db_url

settings = Settings()

# Собираем URL из настроек/переменных (оставляем гибкость под ваши имена)
raw_url = (
    getattr(settings, "database_url", None)
    or getattr(settings, "postgres_url", None)
    or os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
)

db_url = normalize_db_url(raw_url)

if not db_url:
    raise RuntimeError(
        "DATABASE_URL/POSTGRES_URL не задан. Установите переменную Railway или проверьте Settings."
    )

# ВАЖНО: теперь URL уже в виде postgresql+psycopg2://...
engine = create_engine(db_url, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def init_db():
    """
    Если у вас есть модели — импортируйте их ниже, чтобы Base.metadata.create_all
    увидел таблицы. Иначе оставьте как есть.
    """
    # from bot.db import models  # пример
    Base.metadata.create_all(bind=engine)
