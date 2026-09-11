"""Release archived site addresses and prefixes while preserving their history."""
import sqlalchemy as sa
from alembic import op

revision = '6c28f3a71e90'
down_revision = '4f8b27c93a10'
branch_labels = None
depends_on = None


def upgrade():
    # Keep uniqueness enforced throughout the transaction, including concurrent
    # site creation. No archived identities, credentials or foreign keys change.
    op.execute('LOCK TABLE sites IN ACCESS EXCLUSIVE MODE')
    for column in ('prefix', 'base_url'):
        op.create_index(f'uq_sites_active_{column}', 'sites', [column], unique=True,
                        postgresql_where=sa.text('archived = false'))
        op.drop_constraint(f'sites_{column}_key', 'sites', type_='unique')


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text('LOCK TABLE sites IN ACCESS EXCLUSIVE MODE'))
    # Never rename or remove history to make an older schema fit. Reject the
    # downgrade before DDL if identities have been reused since this upgrade.
    for column in ('prefix', 'base_url'):
        if connection.scalar(sa.text(
                f'SELECT EXISTS (SELECT 1 FROM sites GROUP BY {column} HAVING count(*) > 1)')):
            raise RuntimeError('Cannot restore global site uniqueness after an archived identity has been reused')
    for column in ('prefix', 'base_url'):
        op.create_unique_constraint(f'sites_{column}_key', 'sites', [column])
        op.drop_index(f'uq_sites_active_{column}', table_name='sites')
