"""add dialog kb tables and extend kb_documents

Revision ID: 002_add_dialog_kb_tables
Revises: 001_initial
Create Date: 2026-01-03
"""

from alembic import op
import sqlalchemy as sa


revision = "002_add_dialog_kb_tables"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # ---- extend kb_documents ----
    with op.batch_alter_table("kb_documents") as b:
        b.add_column(sa.Column("resource_id", sa.String(length=255), nullable=True))
        b.add_column(sa.Column("md5", sa.String(length=64), nullable=True))
        b.add_column(sa.Column("size", sa.BigInteger(), nullable=True))
        b.add_column(sa.Column("modified_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))

    op.create_index("ix_kb_documents_resource_id", "kb_documents", ["resource_id"], unique=True)

    # ---- dialog_kb_documents ----
    op.create_table(
        "dialog_kb_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_documents_dialog_doc"),
    )
    op.create_index("ix_dialog_kb_documents_dialog_id", "dialog_kb_documents", ["dialog_id"], unique=False)
    op.create_index("ix_dialog_kb_documents_document_id", "dialog_kb_documents", ["document_id"], unique=False)

    # ---- dialog_kb_secrets ----
    op.create_table(
        "dialog_kb_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pdf_password", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dialog_id", "document_id", name="uq_dialog_kb_secrets_dialog_doc"),
    )
    op.create_index("ix_dialog_kb_secrets_dialog_id", "dialog_kb_secrets", ["dialog_id"], unique=False)


def downgrade():
    op.drop_index("ix_dialog_kb_secrets_dialog_id", table_name="dialog_kb_secrets")
    op.drop_table("dialog_kb_secrets")

    op.drop_index("ix_dialog_kb_documents_document_id", table_name="dialog_kb_documents")
    op.drop_index("ix_dialog_kb_documents_dialog_id", table_name="dialog_kb_documents")
    op.drop_table("dialog_kb_documents")

    op.drop_index("ix_kb_documents_resource_id", table_name="kb_documents")

    with op.batch_alter_table("kb_documents") as b:
        b.drop_column("is_active")
        b.drop_column("modified_at")
        b.drop_column("size")
        b.drop_column("md5")
        b.drop_column("resource_id")
