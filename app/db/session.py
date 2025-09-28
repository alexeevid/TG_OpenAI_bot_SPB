from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

class Base(DeclarativeBase):
    pass

def _normalize_db_url(url: str | None) -> str:
    if not url or not url.strip():
        return "sqlite+pysqlite:///:memory:"
    url = url.strip()
    # Railway/Heroku часто отдают DSN вида "postgres://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url

def make_session_factory(database_url: str | None):
    url = _normalize_db_url(database_url)
    engine = create_engine(url, pool_pre_ping=True, future=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine
