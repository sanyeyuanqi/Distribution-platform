"""Freeze usage services and add optional service discount overrides."""
import sqlalchemy as sa
from alembic import op

revision = 'e849c25a6d10'
down_revision = 'da63ef5b08c7'
branch_labels = None
depends_on = None


def _variant(family, schema):
    if family in ('Anthropic', 'OpenAI', 'OpenRouter', 'OpenCode'):
        return ''
    if not isinstance(schema, dict):
        return None
    kind, remote_type = schema.get('type'), schema.get('remote_type')
    if family == 'AWS':
        if remote_type == 33 and kind in ('aws_ak_sk', 'aws_api_key', 'aws_bedrock'):
            return 'bedrock'
        if remote_type == 14 and kind == 'aws_claude':
            return 'aws_claude'
    elif family == 'Azure':
        if remote_type == 3 and kind in ('api_key', 'azure_gpt'):
            return 'azure_gpt'
        if remote_type == 14 and kind == 'azure_claude':
            return 'azure_claude'
    elif family == 'Google':
        if remote_type == 24 and kind == 'api_key':
            return 'ai_studio_gemini'
        if remote_type == 41 and kind in ('vertex_gemini', 'vertex_claude'):
            return kind
    return None


def upgrade():
    op.add_column('discount_versions', sa.Column('service_variant', sa.String(32), nullable=True))
    op.add_column('discount_versions', sa.Column('inherits_category', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('usage_facts', sa.Column('service_variant', sa.String(32), nullable=True))
    # Only fixed business formats can classify old usage safely. Raw legacy
    # Vertex types remain unknown; changing today's model list is not evidence
    # of which service produced a historical amount. Monetary columns, old
    # discount versions and all bill snapshots remain untouched.
    facts = sa.table('usage_facts', sa.column('id'), sa.column('category_id'), sa.column('channel_id'), sa.column('service_variant'))
    categories = sa.table('categories', sa.column('id'), sa.column('family'))
    channels = sa.table('channels', sa.column('id'), sa.column('category_id'), sa.column('format_id'))
    formats = sa.table('credential_formats', sa.column('id'), sa.column('schema_config', sa.JSON()))
    query = sa.select(facts.c.id, categories.c.family, formats.c.schema_config).select_from(
        facts.join(categories, categories.c.id == facts.c.category_id)
        .outerjoin(channels, sa.and_(channels.c.id == facts.c.channel_id, channels.c.category_id == facts.c.category_id))
        .outerjoin(formats, formats.c.id == channels.c.format_id))
    bind = op.get_bind()
    for fact_id, family, schema in bind.execute(query):
        variant = _variant(family, schema)
        if variant is not None:
            bind.execute(facts.update().where(facts.c.id == fact_id).values(service_variant=variant))


def downgrade():
    versions = sa.table('discount_versions', sa.column('service_variant'))
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(versions).where(versions.c.service_variant.is_not(None))):
        raise RuntimeError('Service discount history exists; refusing a lossy downgrade')
    op.drop_column('usage_facts', 'service_variant')
    op.drop_column('discount_versions', 'inherits_category')
    op.drop_column('discount_versions', 'service_variant')
