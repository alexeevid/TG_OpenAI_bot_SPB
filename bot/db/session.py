from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from bot.settings import load_settings

Base = declarative_base()

_settings = load_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def get_session():
    return SessionLocal()
