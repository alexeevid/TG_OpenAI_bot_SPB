
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

class Base(DeclarativeBase): pass

def make_session_factory(database_url: str | None):
    engine = create_engine(database_url, pool_pre_ping=True) if database_url else create_engine("sqlite+pysqlite:///:memory:")
    return sessionmaker(bind=engine, expire_on_commit=False), engine
