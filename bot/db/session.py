from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.settings import settings

DB_URL = settings.database_url

engine = create_engine(DB_URL, echo=True, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db(Base):
    from . import models
    models.Base.metadata.create_all(bind=engine)
