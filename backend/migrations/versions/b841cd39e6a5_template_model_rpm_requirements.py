"""Store per-model receiving demand separately from upstream rate limits."""
import sqlalchemy as sa
from alembic import op

revision = 'b841cd39e6a5'
down_revision = 'a730bc28d594'
branch_labels = None
depends_on = None


def upgrade():
    # The additive default backfills existing rows without changing their
    # template revisions, UUIDs or frozen distribution/task configurations.
    op.add_column('site_upload_templates', sa.Column(
        'model_rpm_requirements', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('site_upload_templates', 'model_rpm_requirements')
