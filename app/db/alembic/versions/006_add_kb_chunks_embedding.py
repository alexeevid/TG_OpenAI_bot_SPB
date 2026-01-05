from alembic import op
import sqlalchemy as sa

revision = "006_kb_chunks_emb"
down_revision = "005_kb_chunks_upd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector должен быть уже установлен (обычно в 001_initial)
    op.execute(
        """
        ALTER TABLE kb_chunks
        ADD COLUMN IF NOT EXISTS embedding VECTOR
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE kb_chunks DROP COLUMN IF EXISTS embedding"
    )
