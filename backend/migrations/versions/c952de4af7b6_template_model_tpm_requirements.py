"""Add independent per-model token demand without rewriting existing RPM data."""
import sqlalchemy as sa
from alembic import op

revision = 'c952de4af7b6'
down_revision = 'b841cd39e6a5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site_upload_templates', sa.Column(
        'model_tpm_requirements', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('site_upload_templates', 'model_tpm_requirements')
