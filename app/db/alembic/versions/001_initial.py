"""Initial schema for bot (users/dialogs/messages) + KB/RAG with pgvector.

- Uses pgvector extension and VECTOR(3072) for OpenAI text-embedding-3-large
- KB documents are synced from Yandex.Disk and attached per-dialog (dialog_kb_documents)
- Per-dialog secrets store PDF passwords (dialog_kb_secrets)

Revision ID: 001_initial
Revises: 
Create Date: 2026-01-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 3072


def upgrade() -> None:
    # pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- core ---
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.Column("active_dialog_id", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_users_tg_id", "users", ["tg_id"], unique=True)

    op.create_table(
        "dialogs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )

    # --- knowledge base / rag ---
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),

        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("md5", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger, nullable=True),
        sa.Column("modified_at", sa.DateTime(timezone=False), nullable=True),

        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),

        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("indexed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_kb_documents_path", "kb_documents", ["path"], unique=True)
    op.create_index("ix_kb_documents_resource_id", "kb_documents", ["resource_id"], unique=True)

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"], unique=False)
    # Vector index (ivfflat). Requires ANALYZE after bulk load; for small KB it still works without.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding_ivfflat "
        f"ON kb_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        "dialog_kb_documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index(
        "ux_dialog_kb_documents_dialog_doc",
        "dialog_kb_documents",
        ["dialog_id", "document_id"],
        unique=True,
    )

    op.create_table(
        "dialog_kb_secrets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("secret_type", sa.String(), nullable=False, server_default="pdf_password"),
        sa.Column("secret_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index(
        "ux_dialog_kb_secrets_dialog_doc_type",
        "dialog_kb_secrets",
        ["dialog_id", "document_id", "secret_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_dialog_kb_secrets_dialog_doc_type", table_name="dialog_kb_secrets")
    op.drop_table("dialog_kb_secrets")

    op.drop_index("ux_dialog_kb_documents_dialog_doc", table_name="dialog_kb_documents")
    op.drop_table("dialog_kb_documents")

    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_ivfflat")
    op.drop_index("ix_kb_chunks_document_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")

    op.drop_index("ix_kb_documents_resource_id", table_name="kb_documents")
    op.drop_index("ix_kb_documents_path", table_name="kb_documents")
    op.drop_table("kb_documents")

    op.drop_table("messages")
    op.drop_table("dialogs")

    op.drop_index("ix_users_tg_id", table_name="users")
    op.drop_table("users")
