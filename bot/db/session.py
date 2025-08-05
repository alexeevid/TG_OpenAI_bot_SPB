from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from bot.settings import settings

DB_URL = settings.database_url

engine = create_engine(DB_URL, echo=True, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Объявление Base здесь
Base = declarative_base()

def init_db():
    # импорт моделей внутри функции, чтобы избежать циклического импорта
    from . import models
    Base.metadata.create_all(bind=engine)
