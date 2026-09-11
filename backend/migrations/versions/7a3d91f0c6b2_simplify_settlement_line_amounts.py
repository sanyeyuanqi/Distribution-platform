"""Keep only settled usage and payment as order-line amount columns."""
import sqlalchemy as sa
from alembic import op

revision = '7a3d91f0c6b2'
down_revision = '19ac74d3e860'
branch_labels = None
depends_on = None


def upgrade():
    # Target snapshots already hold the counters needed to avoid double settlement.
    op.drop_column('settlement_order_lines', 'cumulative_total')
    op.drop_column('settlement_order_lines', 'previous_total')


def downgrade():
    op.add_column('settlement_order_lines', sa.Column('cumulative_total', sa.Numeric(), nullable=True))
    op.add_column('settlement_order_lines', sa.Column('previous_total', sa.Numeric(), nullable=True))
    # Reconstruct the former aggregates from exact frozen active-target amounts.
    # Carried inactive targets were not included in these aggregates.
    op.execute(sa.text("""
        UPDATE settlement_order_lines AS line
        SET cumulative_total = COALESCE((
                SELECT SUM((source->>'cumulative_total')::numeric)
                FROM json_array_elements(line.site_amounts) AS source
                WHERE COALESCE((source->>'active')::boolean, true)
            ), 0),
            previous_total = COALESCE((
                SELECT SUM((source->>'previous_total')::numeric)
                FROM json_array_elements(line.site_amounts) AS source
                WHERE COALESCE((source->>'active')::boolean, true)
            ), 0)
    """))
    op.alter_column('settlement_order_lines', 'cumulative_total', nullable=False)
    op.alter_column('settlement_order_lines', 'previous_total', nullable=False)
