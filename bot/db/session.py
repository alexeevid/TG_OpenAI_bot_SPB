from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.db.base import Base
from bot.settings import load_settings

_engine = None
_SessionLocal = None

def _ensure_engine():
    global _engine, _SessionLocal
    if _engine is None:
        db_url = load_settings().database_url
        _engine = create_engine(db_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)

def get_session():
    _ensure_engine()
    return _SessionLocal()
