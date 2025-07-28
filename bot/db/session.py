from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from bot.settings import Settings

settings = Settings()
engine = create_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db(Base):
    """Создание схемы + автомиграция таблицы documents до актуального вида."""
    Base.metadata.create_all(bind=engine)
    _migrate_documents_table()

def _migrate_documents_table():
    """Добиваемся наличия колонок: title, mime, size, created_at.
    Безопасно для повторных запусков (no-op, если колонки уже есть)."""
    with engine.begin() as conn:
        insp = inspect(conn)
        if 'documents' not in insp.get_table_names():
            return

        cols = {c['name'] for c in insp.get_columns('documents')}

        # title
        if 'title' not in cols:
            conn.execute(text("ALTER TABLE documents ADD COLUMN title VARCHAR(512)"))
            # Заполняем из последнего сегмента пути: 'disk:/Папка/Файл.pdf' -> 'Файл.pdf'
            conn.execute(text(
                "UPDATE documents "
                "SET title = COALESCE(NULLIF(regexp_replace(path, '^.*/', ''), ''), 'Untitled') "
                "WHERE title IS NULL OR title = ''"
            ))

        # mime
        if 'mime' not in cols:
            conn.execute(text("ALTER TABLE documents ADD COLUMN mime VARCHAR(128)"))

        # size
        if 'size' not in cols:
            conn.execute(text("ALTER TABLE documents ADD COLUMN size INTEGER"))

        # created_at
        if 'created_at' not in cols:
            conn.execute(text("ALTER TABLE documents ADD COLUMN created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()"))
            conn.execute(text("UPDATE documents SET created_at = now() WHERE created_at IS NULL"))
