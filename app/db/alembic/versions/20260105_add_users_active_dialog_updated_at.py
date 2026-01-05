from alembic import op
import sqlalchemy as sa

# ВАЖНО: после создания файла поменяешь down_revision (см. ниже)
revision = "20260105_add_users_cols"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users.active_dialog_id
    op.add_column("users", sa.Column("active_dialog_id", sa.Integer(), nullable=True))

    # users.updated_at
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "updated_at")
    op.drop_column("users", "active_dialog_id")
