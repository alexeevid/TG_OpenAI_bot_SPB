"""add voice fields (placeholder)

Revision ID: 002_add_voice_fields
Revises: 001_initial
Create Date: 2025-09-20
"""
from alembic import op
import sqlalchemy as sa

# Alembic identifiers
revision = "002_add_voice_fields"
down_revision = "001_initial"  # проверь точное имя/ID твоей первой миграции
branch_labels = None
depends_on = None

def upgrade():
    # no-op: восстанавливаем цепочку, без изменения схемы
    pass

def downgrade():
    pass
