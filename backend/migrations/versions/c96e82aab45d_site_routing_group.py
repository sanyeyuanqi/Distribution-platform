"""Persist each site's explicitly selected distribution routing group."""
import sqlalchemy as sa
from alembic import op

revision = 'c96e82aab45d'
down_revision = '9df9e14d4a6c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sites', sa.Column('routing_group', sa.String(length=160), nullable=False, server_default='default'))
    op.alter_column('sites', 'routing_group', server_default=None)


def downgrade():
    op.drop_column('sites', 'routing_group')
