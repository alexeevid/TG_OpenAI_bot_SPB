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
# app/db/session.py
from __future__ import annotations

import logging
from sqlalchemy import text

log = logging.getLogger(__name__)


def reset_schema(engine) -> None:
    """
    Полный reset схемы БД: удаляет все таблицы, затем создаёт заново по models.py.
    ВНИМАНИЕ: уничтожает все данные.
    """
    from .models import Base  # важно импортировать здесь, чтобы не было циклов

    log.warning("DB RESET: dropping all tables...")
    Base.metadata.drop_all(bind=engine)

    # На всякий случай: pgvector extension (если используется в KB)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        log.warning("Could not ensure pgvector extension (ok if not used): %s", e)

    log.warning("DB RESET: creating all tables...")
    Base.metadata.create_all(bind=engine)
    log.warning("DB RESET: done.")


def ensure_schema(engine) -> None:
    """
    Мягкая инициализация: создаёт таблицы, если их нет.
    Данные не трогает.
    """
    from .models import Base

    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        log.info("pgvector extension not ensured (ok): %s", e)

    Base.metadata.create_all(bind=engine)

    return sessionmaker(bind=engine, expire_on_commit=False), engine
