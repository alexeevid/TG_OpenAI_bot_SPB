# bot/db/session.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def _normalize_to_psycopg(url: str) -> str:
    """Переводим всё к postgresql+psycopg://…"""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+asyncpg" not in url and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    # если вдруг кто-то подсунул psycopg2 — заменим
    url = url.replace("+psycopg2", "+psycopg")
    url = url.replace("+asyncpg", "+psycopg")
    return url

def _build_url_from_pg_parts(prefix: str = "") -> str | None:
    """
    Собираем DSN вида postgresql+psycopg://user:pass@host:port/db,
    если заданы PGHOST/PGUSER/... либо PUBLIC_PGHOST/... (через prefix).
    """
    get = lambda k: os.getenv(f"{prefix}{k}")  # noqa: E731
    host = get("PGHOST")
    user = get("PGUSER")
    password = get("PGPASSWORD")
    db = get("PGDATABASE")
    port = get("PGPORT") or "5432"

    if all([host, user, password, db]):
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"
    return None

def resolve_database_url() -> str:
    """
    Выбираем URL по приоритетам:
    1) POSTGRES_URL (как в наших настройках)
    2) DATABASE_URL (который создаёт Railway автоконнектор)
    3) Собираем вручную из PGHOST/PGUSER/PGPASSWORD/PGDATABASE (внутренние)
    4) Аналогично из PUBLIC_PGHOST/... (публичные)
    Управлять выбором можно через DB_URL_SOURCE: auto|internal|public
    """
    source = os.getenv("DB_URL_SOURCE", "auto").lower()

    # 1) Явный POSTGRES_URL
    url = os.getenv("POSTGRES_URL")
    if url and (source in ("auto", "internal", "public")):
        return _normalize_to_psycopg(url)

    # 2) DATABASE_URL (Railway default)
    if source in ("auto", "internal", "public"):
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            return _normalize_to_psycopg(db_url)

    # 3) Сборка из внутренних PG* переменных
    if source in ("auto", "internal"):
        url = _build_url_from_pg_parts(prefix="")
        if url:
            return _normalize_to_psycopg(url)

    # 4) Сборка из публичных PUBLIC_PG* переменных
    if source in ("auto", "public"):
        url = _build_url_from_pg_parts(prefix="PUBLIC_")
        if url:
            return _normalize_to_psycopg(url)

    raise RuntimeError("Не удалось определить строку подключения к Postgres. Проверь переменные окружения.")

# ---- Создаём engine / SessionLocal ----
DB_URL = resolve_database_url()
engine = create_engine(DB_URL, pool_pre_ping=True, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
