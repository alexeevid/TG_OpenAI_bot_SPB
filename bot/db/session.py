from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.db.base import Base
from bot.settings import load_settings
import logging, re

_engine = None
_SessionLocal = None

def _mask(url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\\1:***@", url)

def _ensure_engine():
    global _engine, _SessionLocal
    if _engine is None:
        s = load_settings()
        db_url = s.database_url
        logging.getLogger(__name__).info("DB URL resolved: %s", _mask(db_url))
        try:
            _engine = create_engine(db_url, pool_pre_ping=True, future=True)
            _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
        except Exception:
            logging.exception("Failed to create SQLAlchemy engine")
            raise

def get_session():
    _ensure_engine()
    return _SessionLocal()
