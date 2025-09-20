from alembic import op
import sqlalchemy as sa

revision = '002_add_voice_fields'
down_revision = '001_initial'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('dialogs') as batch_op:
        batch_op.add_column(sa.Column('voice_model', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('voice_style', sa.Text(), nullable=True))

def downgrade():
    with op.batch_alter_table('dialogs') as batch_op:
        batch_op.drop_column('voice_style')
        batch_op.drop_column('voice_model')
