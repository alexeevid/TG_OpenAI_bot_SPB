from alembic import op
import sqlalchemy as sa

revision = "004_dialog_kb_fix"
down_revision = "003_dialogs_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Если в проекте реально используются эти таблицы:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dialog_kb_documents (
            id SERIAL PRIMARY KEY,
            dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
            is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(dialog_id, document_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dialog_kb_secrets (
            id SERIAL PRIMARY KEY,
            dialog_id INTEGER NOT NULL REFERENCES dialogs(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
            pdf_password TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(dialog_id, document_id)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dialog_kb_secrets")
    op.execute("DROP TABLE IF EXISTS dialog_kb_documents")
