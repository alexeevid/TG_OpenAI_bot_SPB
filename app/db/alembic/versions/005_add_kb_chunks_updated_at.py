from alembic import op
import sqlalchemy as sa

revision = "005_kb_chunks_upd"
down_revision = "004_dialog_kb_upd"
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
