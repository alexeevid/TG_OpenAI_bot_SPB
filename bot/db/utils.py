# bot/db/utils.py
from __future__ import annotations

def normalize_db_url(url: str | None) -> str | None:
    """
    Приводит ссылку вида postgres://... к формату, который понимает SQLAlchemy:
    postgresql+psycopg2://...

    Также поддерживает postgresql://... -> postgresql+psycopg2://...
    Ничего не делает, если url пустой или уже нормализован.
    """
    if not url:
        return url

    # Railway часто отдаёт postgres://...
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)

    # Иногда встречается postgresql://... (без указания драйвера)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)

    return url
