"""Add snapshot-based settlement orders without rewriting historical bills."""
import sqlalchemy as sa
from alembic import op

revision = '19ac74d3e860'
down_revision = 'f8a24d09b6c1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('settlement_orders',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('number', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('actor_name', sa.String(160), nullable=False),
        sa.Column('payer_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('payer_name', sa.String(160), nullable=False),
        sa.Column('payee_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('payee_name', sa.String(160), nullable=False),
        sa.Column('identity_snapshot', sa.JSON(), nullable=False),
        sa.Column('layer', sa.String(16), nullable=False),
        sa.Column('usage_amount', sa.Numeric(), nullable=False),
        sa.Column('payment_amount', sa.Numeric(), nullable=False),
        sa.Column('pricing_unit', sa.String(16), nullable=False),
        sa.Column('payment_unit', sa.String(16), nullable=False),
        sa.Column('line_count', sa.Integer(), nullable=False),
        sa.Column('idempotency_key', sa.String(100), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('actor_id', 'idempotency_key', name='uq_settlement_order_request'),
        sa.CheckConstraint("status = 'settled'", name='ck_settlement_order_status'),
        sa.CheckConstraint('usage_amount >= 0 AND payment_amount >= 0', name='ck_settlement_order_amounts'))
    for name in ('actor_id', 'payer_id', 'payee_id', 'layer', 'created_at'):
        op.create_index('ix_settlement_orders_' + name, 'settlement_orders', [name])
    op.create_table('settlement_order_lines',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('order_id', sa.String(36), sa.ForeignKey('settlement_orders.id'), nullable=False),
        sa.Column('channel_id', sa.String(36), sa.ForeignKey('channels.id'), nullable=False),
        sa.Column('display_id', sa.BigInteger(), nullable=False),
        sa.Column('group_id', sa.String(36), sa.ForeignKey('upload_groups.id'), nullable=False),
        sa.Column('group_tag', sa.String(80), nullable=False),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('owner_name', sa.String(160), nullable=False),
        sa.Column('owner_username', sa.String(64), nullable=False),
        sa.Column('category_id', sa.String(36), sa.ForeignKey('categories.id'), nullable=False),
        sa.Column('category_name', sa.String(100), nullable=False),
        sa.Column('variant', sa.String(32), nullable=False),
        sa.Column('service_name', sa.String(160), nullable=False),
        sa.Column('service_name_en', sa.String(160), nullable=False),
        sa.Column('discount_id', sa.String(36), sa.ForeignKey('discount_versions.id'), nullable=True),
        sa.Column('discount_percent', sa.Numeric(10, 6), nullable=False),
        sa.Column('usage_amount', sa.Numeric(), nullable=False),
        sa.Column('cumulative_total', sa.Numeric(), nullable=False),
        sa.Column('previous_total', sa.Numeric(), nullable=False),
        sa.Column('payment_amount', sa.Numeric(), nullable=False),
        sa.Column('site_amounts', sa.JSON(), nullable=False),
        sa.UniqueConstraint('order_id', 'channel_id', name='uq_settlement_order_channel'),
        sa.CheckConstraint('discount_percent >= 0 AND discount_percent <= 100', name='ck_settlement_order_discount'),
        sa.CheckConstraint('usage_amount >= 0 AND payment_amount >= 0', name='ck_settlement_order_line_amounts'))
    for name in ('order_id', 'channel_id'):
        op.create_index('ix_settlement_order_lines_' + name, 'settlement_order_lines', [name])


def downgrade():
    op.drop_table('settlement_order_lines')
    op.drop_table('settlement_orders')
