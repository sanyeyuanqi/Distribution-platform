"""Add explicit template model mappings without changing existing configuration."""
import sqlalchemy as sa
from alembic import op

revision = 'da63ef5b08c7'
down_revision = 'c952de4af7b6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site_upload_templates', sa.Column(
        'model_mapping', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('site_upload_templates', 'model_mapping')
