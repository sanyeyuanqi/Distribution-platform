"""Category templates and immutable per-destination upload configuration."""
import sqlalchemy as sa
from alembic import op

revision = 'e71a3429d086'
down_revision = 'c96e82aab45d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('site_upload_templates',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('site_id', sa.String(36), sa.ForeignKey('sites.id'), nullable=False),
        sa.Column('category_id', sa.String(36), sa.ForeignKey('categories.id'), nullable=False),
        sa.Column('format_id', sa.String(36), sa.ForeignKey('credential_formats.id'), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('models', sa.JSON(), nullable=False),
        sa.Column('routing_group', sa.String(160), nullable=False),
        sa.Column('remark', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('site_id', 'category_id', name='uq_upload_template_site_category'))
    op.create_index('ix_site_upload_templates_site_id', 'site_upload_templates', ['site_id'])
    op.create_index('ix_site_upload_templates_category_id', 'site_upload_templates', ['category_id'])
    op.add_column('channels', sa.Column('upload_mode', sa.String(16), nullable=False, server_default='advanced'))
    op.alter_column('channels', 'upload_mode', server_default=None)
    op.add_column('distributions', sa.Column('upload_template_id', sa.String(36), nullable=True))
    op.add_column('distributions', sa.Column('template_version', sa.Integer(), nullable=True))
    op.add_column('distributions', sa.Column('template_snapshot', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('distributions', 'template_snapshot', server_default=None)
    op.add_column('task_items', sa.Column('remote_write_attempted', sa.Boolean(), nullable=False, server_default=sa.false()))
    # Existing attempts have no irreversible write marker; treat them conservatively.
    op.execute("UPDATE task_items SET remote_write_attempted = TRUE WHERE operation != 'sync' AND (attempts > 0 OR stage IN ('create_sent', 'write_sent', 'created_pending_verification', 'updated_pending_verification', 'reconcile', 'complete'))")
    op.alter_column('task_items', 'remote_write_attempted', server_default=None)


def downgrade():
    op.drop_column('task_items', 'remote_write_attempted')
    op.drop_column('distributions', 'template_snapshot')
    op.drop_column('distributions', 'template_version')
    op.drop_column('distributions', 'upload_template_id')
    op.drop_column('channels', 'upload_mode')
    op.drop_table('site_upload_templates')
