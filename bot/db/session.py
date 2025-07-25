
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
if not DB_URL:
    raise RuntimeError("DB_URL / DATABASE_URL / POSTGRES_URL is not set")

engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False)
