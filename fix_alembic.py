# fix_alembic.py
from sqlalchemy import create_engine, text
import os

db = os.environ.get("DATABASE_URL")
if not db:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(db)

with engine.begin() as conn:
    conn.execute(
        text("UPDATE alembic_version SET version_num = '002_add_users_cols'")
    )

print("Alembic version fixed to 002_add_users_cols")
