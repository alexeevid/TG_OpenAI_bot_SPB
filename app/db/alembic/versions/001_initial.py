from alembic import op
import sqlalchemy as sa


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


# Настройка под text-embedding-3-large
EMBEDDING_DIM = 3072


def upgrade():
    # 1) pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2) Core entities
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_users_tg_id", "users", ["tg_id"], unique=True)

    op.create_table(
        "dialogs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_dialogs_user_id", "dialogs", ["user_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),  # user/assistant/system
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_messages_dialog_id", "messages", ["dialog_id"], unique=False)

    # 3) Knowledge Base registry (единый источник истины для БЗ)
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer, primary_key=True),

        # Yandex.Disk stable identifiers
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),

        # Display / metadata
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=True),

        # Sync control
        sa.Column("md5", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("modified_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),

        # PDF protection
        sa.Column("pdf_password_required", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),

        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("uq_kb_documents_path", "kb_documents", ["path"], unique=True)
    op.create_index("ix_kb_documents_is_active", "kb_documents", ["is_active"], unique=False)
    # resource_id не всегда есть, но если есть — удобно уникально
    op.create_index("uq_kb_documents_resource_id", "kb_documents", ["resource_id"], unique=True)

    # 4) KB chunks + embeddings (pgvector)
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("page", sa.Integer, nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.text(f"VECTOR({EMBEDDING_DIM})"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"], unique=False)
    op.create_index("ix_kb_chunks_chunk_order", "kb_chunks", ["document_id", "chunk_order"], unique=False)

    # 5) Dialog ↔ KB включения/исключения документов (пер-диалог)
    op.create_table(
        "dialog_kb_documents",
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("is_included", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_dialog_kb_documents_is_included", "dialog_kb_documents", ["is_included"], unique=False)

    # 6) PDF passwords (строго в рамках диалога)
    op.create_table(
        "dialog_kb_secrets",
        sa.Column("dialog_id", sa.Integer, sa.ForeignKey("dialogs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("pdf_password", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
    )

    # 7) Vector index: HNSW (нет ограничения 2000 измерений)
    # Делаем "мягко": если HNSW недоступен в вашей версии pgvector, просто пропускаем индекс
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
    RAISE NOTICE 'HNSW index was not created (pgvector version may be old). RAG will work but slower until index is available.';
  END;
END$$;
"""
    )


def downgrade():
    # Индекс, если создан
    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")

    op.drop_table("dialog_kb_secrets")
    op.drop_index("ix_dialog_kb_documents_is_included", table_name="dialog_kb_documents")
    op.drop_table("dialog_kb_documents")

    op.drop_index("ix_kb_chunks_chunk_order", table_name="kb_chunks")
    op.drop_index("ix_kb_chunks_document_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")

    op.drop_index("uq_kb_documents_resource_id", table_name="kb_documents")
    op.drop_index("ix_kb_documents_is_active", table_name="kb_documents")
    op.drop_index("uq_kb_documents_path", table_name="kb_documents")
    op.drop_table("kb_documents")

    op.drop_index("ix_messages_dialog_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_dialogs_user_id", table_name="dialogs")
    op.drop_table("dialogs")

    op.drop_index("ix_users_tg_id", table_name="users")
    op.drop_table("users")
