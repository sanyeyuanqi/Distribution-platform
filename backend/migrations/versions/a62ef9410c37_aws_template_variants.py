"""Separate AWS Bedrock and Anthropic proxy templates without changing references."""
import sqlalchemy as sa
from alembic import op

revision = 'a62ef9410c37'
down_revision = 'f492cd0e781a'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('site_upload_templates', sa.Column('variant', sa.String(32), nullable=False, server_default=''))
    op.execute("""UPDATE site_upload_templates SET variant = 'bedrock'
                  WHERE category_id IN (SELECT id FROM categories WHERE family = 'AWS')""")
    op.drop_constraint('uq_upload_template_site_category', 'site_upload_templates', type_='unique')
    op.create_unique_constraint('uq_upload_template_site_category_variant', 'site_upload_templates',
                                ['site_id', 'category_id', 'variant'])


def downgrade():
    # Refuse a lossy downgrade if a site now has both AWS template variants.
    op.create_unique_constraint('uq_upload_template_site_category', 'site_upload_templates', ['site_id', 'category_id'])
    op.drop_constraint('uq_upload_template_site_category_variant', 'site_upload_templates', type_='unique')
    op.drop_column('site_upload_templates', 'variant')
