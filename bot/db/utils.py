# bot/db/utils.py
from __future__ import annotations

def normalize_db_url(url: str | None) -> str | None:
    """
    Преобразует postgres://... -> postgresql+psycopg2://...
    и postgresql://... -> postgresql+psycopg2://...
    Возвращает url без изменений, если он уже нормализован или пустой.
    """
    if not url:
        return url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url
