from __future__ import annotations

import os
import logging

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.session import Base, _normalize_db_url  # noqa: F401
from app.db import models  # noqa: F401  (register models)

log = logging.getLogger(__name__)

config = context.config


def _get_database_url() -> str:
    # Railway uses DATABASE_URL; local may use POSTGRES_DSN
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or ""
    return _normalize_db_url(url)


def run_migrations_offline() -> None:
    url = _get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL/POSTGRES_DSN is not set for Alembic offline mode")

    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_database_url()
    if not url:
        # fallback to alembic.ini if provided
        url = config.get_main_option("sqlalchemy.url") or ""

    if url:
        config.set_main_option("sqlalchemy.url", url)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
