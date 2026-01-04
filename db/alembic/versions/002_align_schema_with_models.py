from alembic import op
import sqlalchemy as sa

revision = "002_align_schema_with_models"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # users
    op.add_column("users", sa.Column("active_dialog_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False))

    # dialogs
    op.add_column("dialogs", sa.Column("settings", sa.JSON(), nullable=True))
    op.add_column("dialogs", sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False))

    # messages
    op.add_column("messages", sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False))

    # dialog_kb_documents
    op.create_table(
        "dialog_kb_documents",
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_documents"),
    )
    op.create_index("ix_dialog_kb_documents_dialog_id", "dialog_kb_documents", ["dialog_id"])
    op.create_index("ix_dialog_kb_documents_document_id", "dialog_kb_documents", ["document_id"])

    # dialog_kb_secrets
    op.create_table(
        "dialog_kb_secrets",
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pdf_password", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_secrets"),
    )


def downgrade():
    op.drop_table("dialog_kb_secrets")
    op.drop_index("ix_dialog_kb_documents_document_id", table_name="dialog_kb_documents")
    op.drop_index("ix_dialog_kb_documents_dialog_id", table_name="dialog_kb_documents")
    op.drop_table("dialog_kb_documents")

    op.drop_column("messages", "updated_at")

    op.drop_column("dialogs", "updated_at")
    op.drop_column("dialogs", "settings")

    op.drop_column("users", "updated_at")
    op.drop_column("users", "active_dialog_id")
