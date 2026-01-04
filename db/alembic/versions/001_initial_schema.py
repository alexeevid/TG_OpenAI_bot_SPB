from alembic import op
import sqlalchemy as sa


# Единый initial под текущие ORM-модели app/db/models.py
revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), server_default=sa.text("'user'"), nullable=False),
        sa.Column("active_dialog_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("tg_id", name="uq_users_tg_id"),
    )
    op.create_index("ix_users_tg_id", "users", ["tg_id"], unique=True)

    # --- dialogs ---
    op.create_table(
        "dialogs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_dialogs_user_id", "dialogs", ["user_id"], unique=False)

    # --- messages ---
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_messages_dialog_id", "messages", ["dialog_id"], unique=False)

    # --- kb_documents ---
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("md5", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("modified_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("path", name="uq_kb_documents_path"),
        sa.UniqueConstraint("resource_id", name="uq_kb_documents_resource_id"),
    )
    op.create_index("ix_kb_documents_path", "kb_documents", ["path"], unique=True)
    op.create_index("ix_kb_documents_resource_id", "kb_documents", ["resource_id"], unique=True)

    # --- kb_chunks ---
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=False),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"], unique=False)

    # --- dialog_kb_documents ---
    op.create_table(
        "dialog_kb_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_documents"),
    )
    op.create_index("ix_dialog_kb_documents_dialog_id", "dialog_kb_documents", ["dialog_id"], unique=False)
    op.create_index("ix_dialog_kb_documents_document_id", "dialog_kb_documents", ["document_id"], unique=False)

    # --- dialog_kb_secrets ---
    op.create_table(
        "dialog_kb_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pdf_password", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_secrets"),
    )
    op.create_index("ix_dialog_kb_secrets_dialog_id", "dialog_kb_secrets", ["dialog_id"], unique=False)
    op.create_index("ix_dialog_kb_secrets_document_id", "dialog_kb_secrets", ["document_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_dialog_kb_secrets_document_id", table_name="dialog_kb_secrets")
    op.drop_index("ix_dialog_kb_secrets_dialog_id", table_name="dialog_kb_secrets")
    op.drop_table("dialog_kb_secrets")

    op.drop_index("ix_dialog_kb_documents_document_id", table_name="dialog_kb_documents")
    op.drop_index("ix_dialog_kb_documents_dialog_id", table_name="dialog_kb_documents")
    op.drop_table("dialog_kb_documents")

    op.drop_index("ix_kb_chunks_document_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")

    op.drop_index("ix_kb_documents_resource_id", table_name="kb_documents")
    op.drop_index("ix_kb_documents_path", table_name="kb_documents")
    op.drop_table("kb_documents")

    op.drop_index("ix_messages_dialog_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_dialogs_user_id", table_name="dialogs")
    op.drop_table("dialogs")

    op.drop_index("ix_users_tg_id", table_name="users")
    op.drop_table("users")
