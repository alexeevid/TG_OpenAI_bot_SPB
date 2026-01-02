from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

try:
    from pgvector.psycopg2 import register_vector
except Exception:
    register_vector = None


class Base(DeclarativeBase):
    pass


def _normalize_db_url(url: str | None) -> str:
    if not url or not url.strip():
        return "sqlite+pysqlite:///:memory:"
    url = url.strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def make_session_factory(database_url: str | None):
    url = _normalize_db_url(database_url)
    engine = create_engine(url, pool_pre_ping=True, future=True)

    if register_vector is not None:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, connection_record):  # noqa: ARG001
            try:
                register_vector(dbapi_connection)
            except Exception:
                pass

    return sessionmaker(bind=engine, expire_on_commit=False), engine
