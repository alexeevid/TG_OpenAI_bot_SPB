from alembic import op
import sqlalchemy as sa

revision = "004_add_dialog_kb_updated_at"
down_revision = "003_add_dialogs_settings_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE dialog_kb
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE dialog_kb DROP COLUMN IF EXISTS updated_at"
    )
