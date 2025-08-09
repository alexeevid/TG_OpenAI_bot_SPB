import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.settings import settings

logger = logging.getLogger(__name__)

# Исправляем старый формат postgres:// на postgresql://
DB_URL = settings.database_url
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Маскируем пароль в логах
safe_url = DB_URL.replace(settings.database_url.split('@')[0], "postgresql://***:***")

try:
    engine = create_engine(DB_URL, echo=False, pool_pre_ping=True)
    logger.info(f"📦 Database engine created for {safe_url}")
except Exception as e:
    logger.exception(f"❌ Failed to create engine for {safe_url}")
    raise

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db(Base):
    """Инициализация всех таблиц БД"""
    from . import models
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Database tables created successfully")
    except Exception as e:
        logger.exception("❌ Failed to create database tables")
        raise
