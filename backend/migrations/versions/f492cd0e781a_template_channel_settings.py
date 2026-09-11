"""Persist template channel defaults and encrypted proxy configuration."""
import sqlalchemy as sa
from alembic import op

revision = 'f492cd0e781a'
down_revision = 'e71a3429d086'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site_upload_templates', sa.Column('channel_config', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('site_upload_templates', 'channel_config', server_default=None)
    op.add_column('site_upload_templates', sa.Column('proxy_encrypted', sa.Text(), nullable=True))
    op.add_column('channels', sa.Column('upload_settings', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('channels', 'upload_settings', server_default=None)
    op.add_column('task_items', sa.Column('proxy_encrypted', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('task_items', 'proxy_encrypted')
    op.drop_column('channels', 'upload_settings')
    op.drop_column('site_upload_templates', 'proxy_encrypted')
    op.drop_column('site_upload_templates', 'channel_config')
