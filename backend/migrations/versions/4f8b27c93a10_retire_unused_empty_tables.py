"""Retire unused empty billing, planning and sample tables without losing history."""
import sqlalchemy as sa
from alembic import op

revision = '4f8b27c93a10'
down_revision = '7a3d91f0c6b2'
branch_labels = None
depends_on = None

# Child tables precede their parents; PostgreSQL enforces every remaining FK.
RETIRED_TABLES = (
    'settlement_claims', 'bill_lines', 'adjustments', 'usage_samples', 'payments',
    'attachments', 'bills', 'payment_rules', 'model_targets',
)


def upgrade():
    connection = op.get_bind()
    # Hold all locks before inspecting or dropping anything. A concurrent legacy
    # writer cannot insert history between the emptiness check and the DDL.
    names = ', '.join('"' + name + '"' for name in sorted(RETIRED_TABLES))
    connection.execute(sa.text('LOCK TABLE ' + names + ' IN ACCESS EXCLUSIVE MODE'))
    occupied = [name for name in RETIRED_TABLES
                if connection.scalar(sa.text('SELECT EXISTS (SELECT 1 FROM "' + name + '" LIMIT 1)'))]
    if occupied:
        raise RuntimeError('Refusing to remove non-empty historical tables: ' + ', '.join(occupied))
    for name in RETIRED_TABLES:
        op.drop_table(name)


def downgrade():
    # Only an empty legacy schema can have passed upgrade. Recreate the exact
    # prior definitions, indexes and constraints; current orders are untouched.
    op.create_table('model_targets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('category_id', sa.String(length=36), nullable=False),
    sa.Column('site_id', sa.String(length=36), nullable=True),
    sa.Column('valid_hours', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('payment_rules',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('pricing_unit', sa.String(length=40), nullable=False),
    sa.Column('payment_unit', sa.String(length=40), nullable=False),
    sa.Column('factor', sa.Numeric(precision=28, scale=12), nullable=False),
    sa.Column('basis', sa.Text(), nullable=False),
    sa.Column('precision', sa.Integer(), nullable=False),
    sa.Column('creator_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('bills',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('number', sa.String(length=64), nullable=False),
    sa.Column('layer', sa.String(length=16), nullable=False),
    sa.Column('payer_id', sa.String(length=36), nullable=False),
    sa.Column('payee_id', sa.String(length=36), nullable=False),
    sa.Column('category_id', sa.String(length=36), nullable=False),
    sa.Column('category_name', sa.String(length=160), nullable=False),
    sa.Column('payer_name', sa.String(length=160), nullable=False),
    sa.Column('payee_name', sa.String(length=160), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('pricing_unit', sa.String(length=40), nullable=False),
    sa.Column('payment_unit', sa.String(length=40), nullable=False),
    sa.Column('base_amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('pricing_amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('payment_amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('payment_rule_id', sa.String(length=36), nullable=True),
    sa.Column('payment_factor', sa.Numeric(precision=28, scale=12), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('snapshot_hash', sa.String(length=64), nullable=False),
    sa.Column('start_at', sa.DateTime(), nullable=True),
    sa.Column('end_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('confirm_key', sa.String(length=100), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ),
    sa.ForeignKeyConstraint(['payee_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['payer_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['payment_rule_id'], ['payment_rules.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('confirm_key'),
    sa.UniqueConstraint('number')
    )
    op.create_index(op.f('ix_bills_category_id'), 'bills', ['category_id'], unique=False)
    op.create_index(op.f('ix_bills_layer'), 'bills', ['layer'], unique=False)
    op.create_index(op.f('ix_bills_payee_id'), 'bills', ['payee_id'], unique=False)
    op.create_index(op.f('ix_bills_payer_id'), 'bills', ['payer_id'], unique=False)
    op.create_index(op.f('ix_bills_status'), 'bills', ['status'], unique=False)
    op.create_table('attachments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('bill_id', sa.String(length=36), nullable=False),
    sa.Column('filename', sa.String(length=200), nullable=False),
    sa.Column('storage_name', sa.String(length=100), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('creator_id', sa.String(length=36), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('storage_name')
    )
    op.create_index(op.f('ix_attachments_bill_id'), 'attachments', ['bill_id'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('bill_id', sa.String(length=36), nullable=False),
    sa.Column('payer_id', sa.String(length=36), nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('payment_type', sa.String(length=60), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('unit', sa.String(length=40), nullable=False),
    sa.Column('paid_at', sa.DateTime(), nullable=False),
    sa.Column('network', sa.String(length=100), nullable=False),
    sa.Column('transaction_id', sa.String(length=200), nullable=False),
    sa.Column('recipient_account', sa.String(length=500), nullable=False),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['payer_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bill_id'),
    sa.UniqueConstraint('idempotency_key'),
    sa.UniqueConstraint('payer_id', 'network', 'transaction_id', name='uq_payment_transaction')
    )
    op.create_table('usage_samples',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('distribution_id', sa.String(length=36), nullable=False),
    sa.Column('source_id', sa.String(length=200), nullable=False),
    sa.Column('amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('unit', sa.String(length=40), nullable=False),
    sa.Column('delta', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('status', sa.String(length=40), nullable=False),
    sa.Column('sampled_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['distribution_id'], ['distributions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('distribution_id', 'source_id', name='uq_usage_sample')
    )
    op.create_table('adjustments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('bill_id', sa.String(length=36), nullable=False),
    sa.Column('fact_id', sa.String(length=36), nullable=True),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('unit', sa.String(length=40), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('creator_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['fact_id'], ['usage_facts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_index(op.f('ix_adjustments_bill_id'), 'adjustments', ['bill_id'], unique=False)
    op.create_table('bill_lines',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('bill_id', sa.String(length=36), nullable=False),
    sa.Column('fact_id', sa.String(length=36), nullable=False),
    sa.Column('base_amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('percent', sa.Numeric(precision=10, scale=6), nullable=False),
    sa.Column('pricing_amount', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.Column('discount_id', sa.String(length=36), nullable=True),
    sa.Column('original_discount_id', sa.String(length=36), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['discount_id'], ['discount_versions.id'], ),
    sa.ForeignKeyConstraint(['fact_id'], ['usage_facts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bill_lines_bill_id'), 'bill_lines', ['bill_id'], unique=False)
    op.create_table('settlement_claims',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('layer', sa.String(length=16), nullable=False),
    sa.Column('payer_id', sa.String(length=36), nullable=False),
    sa.Column('payee_id', sa.String(length=36), nullable=False),
    sa.Column('fact_id', sa.String(length=36), nullable=False),
    sa.Column('bill_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], ),
    sa.ForeignKeyConstraint(['fact_id'], ['usage_facts.id'], ),
    sa.ForeignKeyConstraint(['payee_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['payer_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('layer', 'payer_id', 'payee_id', 'fact_id', name='uq_settlement_fact_claim')
    )
    op.create_index(op.f('ix_settlement_claims_bill_id'), 'settlement_claims', ['bill_id'], unique=False)
