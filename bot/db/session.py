import logging
import re
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.settings import settings

logger = logging.getLogger(__name__)

# 1) Нормализуем URL (postgres:// → postgresql://)
DB_URL = settings.database_url
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql://" + DB_URL[len("postgres://"):]

def _mask(url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url or "")

try:
    engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
    logger.info("DB URL resolved: %s", _mask(DB_URL))
except Exception:
    logger.exception("Failed to create SQLAlchemy engine")
    raise

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
