from alembic import op
import sqlalchemy as sa

revision = "002_add_users_cols"
down_revision = "001_initial"  # IMPORTANT: см. ниже
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("active_dialog_id", sa.Integer(), nullable=True))
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "updated_at")
    op.drop_column("users", "active_dialog_id")
