from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 3072


def upgrade():
    # pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_users_tg_id", "users", ["tg_id"], unique=True)

    # dialogs
    op.create_table(
        "dialogs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_dialogs_user_id", "dialogs", ["user_id"])

    # messages
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_messages_dialog_id", "messages", ["dialog_id"])

    # kb_documents
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("md5", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("modified_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("pdf_password_required", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("uq_kb_documents_path", "kb_documents", ["path"], unique=True)
    op.create_index("uq_kb_documents_resource_id", "kb_documents", ["resource_id"], unique=True)
    op.create_index("ix_kb_documents_is_active", "kb_documents", ["is_active"])

    # kb_chunks (pgvector)
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])
    op.create_index("ix_kb_chunks_chunk_order", "kb_chunks", ["document_id", "chunk_order"])

    # dialog_kb_documents
    op.create_table(
        "dialog_kb_documents",
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("is_included", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )

    # dialog_kb_secrets
    op.create_table(
        "dialog_kb_secrets",
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("pdf_password", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )

    # HNSW index (safe)
    op.execute(
        """
DO $$
BEGIN
  BEGIN
    EXECUTE 'CREATE INDEX ix_kb_chunks_embedding_hnsw
             ON kb_chunks
             USING hnsw (embedding vector_cosine_ops)
             WITH (m = 16, ef_construction = 200)';
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'HNSW index was not created. RAG will work without index.';
  END;
END$$;
"""
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")

    op.drop_table("dialog_kb_secrets")
    op.drop_table("dialog_kb_documents")

    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")

    op.drop_table("messages")
    op.drop_table("dialogs")
    op.drop_table("users")
