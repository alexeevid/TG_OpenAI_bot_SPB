from alembic import context
from sqlalchemy import engine_from_config, pool
from logging.config import fileConfig
import os, sys

# /app/alembic -> /app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.db.base import Base
import bot.db.models  # noqa: F401

config = context.config

# ВАЖНО: сразу прокидываем URL из ENV, не вызывая get_main_option()
db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
if db_url:
    # SQLAlchemy ожидает префикс postgresql://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option('sqlalchemy.url')
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
