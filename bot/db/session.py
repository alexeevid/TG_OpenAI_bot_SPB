from __future__ import annotations

import logging
import re
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.settings import load_settings

log = logging.getLogger(__name__)

# Normalize URL (postgres:// -> postgresql://) and mask password for logs
_settings = load_settings()
DB_URL = _settings.database_url
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql://" + DB_URL[len("postgres://"):]

def _mask(url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url or "")

try:
    engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
    log.info("DB URL resolved: %s", _mask(DB_URL))
except Exception:
    log.exception("Failed to create SQLAlchemy engine")
    raise

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
