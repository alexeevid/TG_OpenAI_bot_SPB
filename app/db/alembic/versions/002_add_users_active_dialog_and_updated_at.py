from alembic import op
import sqlalchemy as sa

revision = "002_add_users_cols"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # 1) active_dialog_id
    op.add_column("users", sa.Column("active_dialog_id", sa.Integer(), nullable=True))

    # 2) updated_at (ставим NOW() по умолчанию, чтобы старые записи не ломались)
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )

    # Опционально: убрать server_default после заполнения (не обязательно)
    # op.alter_column("users", "updated_at", server_default=None)


def downgrade():
    op.drop_column("users", "updated_at")
    op.drop_column("users", "active_dialog_id")
