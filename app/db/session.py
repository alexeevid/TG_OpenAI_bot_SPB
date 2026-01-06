# app/db/session.py
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

try:
    from pgvector.psycopg2 import register_vector  # type: ignore
except Exception:
    register_vector = None

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _normalize_db_url(url: str | None) -> str:
    """
    Railway иногда отдаёт DATABASE_URL в старом формате postgres://
    SQLAlchemy + psycopg2 ожидают postgresql+psycopg2://
    """
    if not url or not url.strip():
        return "sqlite+pysqlite:///:memory:"
    url = url.strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def make_session_factory(database_url: str | None):
    """
    Возвращает (session_factory, engine).
    """
    url = _normalize_db_url(database_url)
    engine = create_engine(url, pool_pre_ping=True, future=True)

    if register_vector is not None:

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, connection_record):  # noqa: ARG001
            try:
                register_vector(dbapi_connection)
            except Exception:
                pass

    SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return SessionFactory, engine


def reset_schema(engine: Engine) -> None:
    """
    Полный reset схемы БД: удаляет все таблицы, затем создаёт заново по models.py.
    ВНИМАНИЕ: уничтожает все данные.
    """
    from .models import Base as ModelsBase  # важно: модели импортируют Base из session.py

    log.warning("DB RESET: dropping all tables...")
    ModelsBase.metadata.drop_all(bind=engine)

    # pgvector extension (если используется)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        log.warning("Could not ensure pgvector extension (ok if not used): %s", e)

    log.warning("DB RESET: creating all tables...")
    ModelsBase.metadata.create_all(bind=engine)
    log.warning("DB RESET: done.")


def ensure_schema(engine: Engine) -> None:
    """
    Мягкая инициализация: создаёт таблицы, если их нет. Данные не трогает.
    """
    from .models import Base as ModelsBase

    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        log.info("pgvector extension not ensured (ok): %s", e)

    ModelsBase.metadata.create_all(bind=engine)
