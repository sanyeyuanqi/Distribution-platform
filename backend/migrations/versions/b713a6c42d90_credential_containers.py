"""Keep a local upload container with separately authenticated remote partitions."""
import sqlalchemy as sa
from alembic import op

revision = 'b713a6c42d90'
down_revision = 'a62ef9410c37'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channels', sa.Column('key_mode', sa.String(16), nullable=False, server_default='single'))
    op.add_column('channels', sa.Column('key_count', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('distributions', sa.Column('partition_key', sa.String(64), nullable=False, server_default=''))
    op.add_column('distributions', sa.Column('partition_label', sa.String(160), nullable=False, server_default=''))
    op.add_column('distributions', sa.Column('key_count', sa.Integer(), nullable=False, server_default='1'))
    op.drop_constraint('uq_distribution_channel_site', 'distributions', type_='unique')
    op.create_unique_constraint('uq_distribution_channel_site_partition', 'distributions',
                                ['channel_id', 'site_id', 'partition_key'])
    op.create_table('channel_credentials',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('channel_id', sa.String(36), sa.ForeignKey('channels.id'), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(128), nullable=False),
        sa.UniqueConstraint('owner_id', 'fingerprint', name='uq_member_owner_fingerprint'),
        sa.UniqueConstraint('channel_id', 'ordinal', name='uq_member_channel_ordinal'))
    for column in ('owner_id', 'channel_id', 'fingerprint'):
        op.create_index('ix_channel_credentials_' + column, 'channel_credentials', [column])
    # Existing single-key identities can be indexed without decrypting or
    # rewriting a single historical credential, version, or task snapshot.
    op.execute('INSERT INTO channel_credentials (id, owner_id, channel_id, ordinal, fingerprint) '
               'SELECT id, owner_id, id, 0, fingerprint FROM channels')


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM channels WHERE key_mode <> 'single' LIMIT 1")).first():
        raise RuntimeError('Cannot downgrade while credential containers exist')
    op.drop_table('channel_credentials')
    op.drop_constraint('uq_distribution_channel_site_partition', 'distributions', type_='unique')
    op.create_unique_constraint('uq_distribution_channel_site', 'distributions', ['channel_id', 'site_id'])
    for column in ('partition_key', 'partition_label', 'key_count'):
        op.drop_column('distributions', column)
    op.drop_column('channels', 'key_count')
    op.drop_column('channels', 'key_mode')
