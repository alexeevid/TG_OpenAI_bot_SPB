from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

DB_URL = os.getenv("DATABASE_URL")

engine = create_engine(DB_URL) if DB_URL else None
SessionLocal = sessionmaker(bind=engine) if engine else None
Base = declarative_base()
