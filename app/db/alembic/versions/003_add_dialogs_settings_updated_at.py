from alembic import op
import sqlalchemy as sa

revision = "003_dialogs_cols"
down_revision = "002_add_users_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # dialogs.settings (JSONB) — дефолт пустой объект
    op.execute(
        """
        ALTER TABLE dialogs
        ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )

    # dialogs.updated_at — дефолт now()
    op.execute(
        """
        ALTER TABLE dialogs
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )


def downgrade() -> None:
    # В downgrade IF EXISTS полезен, чтобы не падать
    op.execute("ALTER TABLE dialogs DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE dialogs DROP COLUMN IF EXISTS settings")
