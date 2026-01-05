from alembic import op

# НЕ МЕНЯЙ revision/down_revision, если они уже “в цепочке” у тебя на проде.
# Оставь как в текущем файле и просто перепиши upgrade()/downgrade().
# Ниже я показываю только функции — вставь их в существующий файл.

def upgrade() -> None:
    # Таблица связи диалог ↔ документ БЗ
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

    # Таблица секретов (пароли PDF) в рамках диалога
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
