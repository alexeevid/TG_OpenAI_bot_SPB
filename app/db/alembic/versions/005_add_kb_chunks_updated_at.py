from alembic import op
import sqlalchemy as sa

revision = "005_add_kb_chunks_updated_at"
down_revision = "004_add_dialog_kb_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb_chunks
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE kb_chunks DROP COLUMN IF EXISTS updated_at"
    )
