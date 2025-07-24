from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.config import load_settings
_settings = load_settings()
engine = create_engine(_settings.postgres_url, pool_pre_ping=True, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
