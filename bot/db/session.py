from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from bot.settings import Settings

settings = Settings()
engine = create_engine(settings.database_url, echo=False, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db(Base):
    Base.metadata.create_all(bind=engine)
