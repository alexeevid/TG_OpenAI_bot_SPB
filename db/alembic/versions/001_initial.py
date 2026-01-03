
from alembic import op
import sqlalchemy as sa

revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('users',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('tg_id', sa.String(), nullable=True),
        sa.Column('role', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()'))
    )
    op.create_index('ix_users_tg_id','users',['tg_id'], unique=True)

    op.create_table('dialogs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id')),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()'))
    )

    op.create_table('messages',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('dialog_id', sa.Integer, sa.ForeignKey('dialogs.id')),
        sa.Column('role', sa.String(), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('NOW()'))
    )

    op.create_table('kb_documents',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('path', sa.String(), nullable=False, unique=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('NOW()'))
    )

    op.create_table('kb_chunks',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('document_id', sa.Integer, sa.ForeignKey('kb_documents.id')),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('embedding', sa.Text(), nullable=False)  # json-строка
    )

def downgrade():
    op.drop_table('kb_chunks')
    op.drop_table('kb_documents')
    op.drop_table('messages')
    op.drop_table('dialogs')
    op.drop_index('ix_users_tg_id', table_name='users')
    op.drop_table('users')
