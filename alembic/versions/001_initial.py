from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# Alembic identifiers
revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # pgvector (может потребовать прав superuser на БД)
    op.execute('CREATE EXTENSION IF NOT EXISTS vector;')

    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('tg_user_id', sa.BigInteger, unique=True, nullable=False),
        sa.Column('is_admin', sa.Boolean, server_default=sa.text('false'), nullable=False),
        sa.Column('is_allowed', sa.Boolean, server_default=sa.text('true'), nullable=False),
        sa.Column('lang', sa.String(10), server_default='ru', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'dialogs',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('user_id', sa.BigInteger, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.Text),
        sa.Column('style', sa.String(20), server_default='expert', nullable=False),
        sa.Column('model', sa.Text),
        sa.Column('is_deleted', sa.Boolean, server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_message_at', sa.DateTime(timezone=True)),
    )

    op.create_table(
        'messages',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('dialog_id', sa.BigInteger, sa.ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('tokens', sa.Integer),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'kb_documents',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('path', sa.Text, unique=True, nullable=False),
        sa.Column('etag', sa.Text),
        sa.Column('mime', sa.Text),
        sa.Column('pages', sa.Integer),
        sa.Column('bytes', sa.BigInteger),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true'), nullable=False),
    )

    op.create_table(
        'kb_chunks',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('document_id', sa.BigInteger, sa.ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chunk_index', sa.Integer, nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('meta', sa.JSON),
        sa.Column('embedding', Vector(dim=3072)),
    )
    op.create_index('ix_kb_chunks_document_id', 'kb_chunks', ['document_id'])
    op.execute('CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks USING ivfflat (embedding vector_cosine_ops);')

    op.create_table(
        'dialog_kb_links',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('dialog_id', sa.BigInteger, sa.ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_id', sa.BigInteger, sa.ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'pdf_passwords',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('dialog_id', sa.BigInteger, sa.ForeignKey('dialogs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_id', sa.BigInteger, sa.ForeignKey('kb_documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('pwd_hash', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('user_id', sa.BigInteger, sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('event', sa.Text, nullable=False),
        sa.Column('payload', sa.JSON),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )


def downgrade():
    op.drop_table('audit_log')
    op.drop_table('pdf_passwords')
    op.drop_table('dialog_kb_links')
    op.execute('DROP INDEX IF EXISTS kb_chunks_embedding_idx;')
    op.drop_index('ix_kb_chunks_document_id', table_name='kb_chunks')
    op.drop_table('kb_chunks')
    op.drop_table('kb_documents')
    op.drop_table('messages')
    op.drop_table('dialogs')
    op.drop_table('users')
