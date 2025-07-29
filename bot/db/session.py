from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bot.config import load_settings

# Загружаем настройки и нормализованный DATABASE_URL
settings = load_settings()

# Railway часто подставляет postgresql:// - SQLAlchemy сам подберет подходящий драйвер (psycopg)
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

# Фабрика сессий
SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

def init_db(base=None) -> None:
    """
    Инициализация схемы БД.

    Совместима с двумя стилями вызова:
      - init_db()                      # новый стиль (Base определяется из моделей)
      - init_db(Base)                  # старый стиль (передается DeclarativeBase)

    :param base: DeclarativeBase (опционально)
    """
    if base is None:
        # Импортируем здесь, чтобы избежать циклических импортов
        from .models import Base as _Base
        base = _Base

    base.metadata.create_all(bind=engine)
