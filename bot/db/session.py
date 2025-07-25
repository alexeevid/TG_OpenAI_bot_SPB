
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.config import load_settings

settings = load_settings()
DB_URL = settings.database_url or "sqlite:///./local.sqlite3"

engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def init_db(Base):
    logging.info("Creating DB schema if not exists...")
    Base.metadata.create_all(engine)
