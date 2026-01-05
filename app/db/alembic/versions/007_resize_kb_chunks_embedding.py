from alembic import op

revision = "007_kb_emb_dim"
down_revision = "006_kb_chunks_emb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Приводим embedding к размерности 3072
    op.execute(
        """
        ALTER TABLE kb_chunks
        ALTER COLUMN embedding TYPE VECTOR(3072)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE kb_chunks
        ALTER COLUMN embedding TYPE VECTOR
        """
    )
