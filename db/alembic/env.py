from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.session import Base
from app.db import models  # noqa: F401  (важно: импорт моделей для регистрации metadata)


# Alembic Config object, which provides access to the values within the .ini file in use.
config = context.config

# Configure Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ----
# IMPORTANT: Railway передаёт креды БД через env (DATABASE_URL).
# Alembic.ini не должен пытаться интерполировать %(DATABASE_URL)s — это ломает configparser.
# Подставляем URL программно.
# ----
db_url = os.getenv("DATABASE_URL")
if not db_url:
    raise RuntimeError("DATABASE_URL is not set (Railway Variables).")

# Если вдруг в окружении окажется старый формат с +psycopg (не ваш кейс, но безопасно),
# можно мягко нормализовать под psycopg2-binary:
# db_url = db_url.replace("postgresql+psycopg://", "postgresql://")

config.set_main_option("sqlalchemy.url", db_url)

# Metadata for 'autogenerate' support.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
