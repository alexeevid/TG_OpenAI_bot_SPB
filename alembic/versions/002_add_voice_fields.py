
from alembic import op
import sqlalchemy as sa

revision = '002_add_voice_fields'
down_revision = '001_initial'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('dialogs', sa.Column('voice_model', sa.Text(), nullable=True))
    op.add_column('dialogs', sa.Column('voice_style', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('dialogs', 'voice_style')
    op.drop_column('dialogs', 'voice_model')
