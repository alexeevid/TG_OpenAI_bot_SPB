import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def _normalize_to_psycopg(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+asyncpg" not in url and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    url = url.replace("+psycopg2", "+psycopg").replace("+asyncpg", "+psycopg")
    return url

def _build_url(prefix: str = "") -> str | None:
    get = lambda k: os.getenv(f"{prefix}{k}")
    host = get("PGHOST")
    user = get("PGUSER")
    password = get("PGPASSWORD")
    db = get("PGDATABASE")
    port = get("PGPORT") or "5432"
    if all([host, user, password, db]):
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"
    return None

def resolve_database_url() -> str:
    source = os.getenv("DB_URL_SOURCE", "auto").lower()
    url = os.getenv("POSTGRES_URL")
    if url and source in ("auto", "internal", "public"):
        return _normalize_to_psycopg(url)

    if source in ("auto", "internal", "public"):
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            return _normalize_to_psycopg(db_url)

    if source in ("auto", "internal"):
        url = _build_url("")
        if url:
            return _normalize_to_psycopg(url)

    if source in ("auto", "public"):
        url = _build_url("PUBLIC_")
        if url:
            return _normalize_to_psycopg(url)

    raise RuntimeError("Cannot resolve Postgres URL")

DB_URL = resolve_database_url()
engine = create_engine(DB_URL, pool_pre_ping=True, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
