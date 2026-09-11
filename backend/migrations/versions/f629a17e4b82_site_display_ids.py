"""Assign persistent numeric site labels without changing UUID references."""
import sqlalchemy as sa
from alembic import op

revision = 'f629a17e4b82'
down_revision = 'e508cb6a721f'
branch_labels = None
depends_on = None


def upgrade():
    # ALTER holds the table lock until commit, preventing inserts from racing
    # the stable backfill (including archived sites) and sequence initialization.
    op.add_column('sites', sa.Column('display_id', sa.BigInteger(), nullable=True))
    op.execute('WITH numbered AS ('
               'SELECT id, row_number() OVER (ORDER BY created_at, id) AS number FROM sites'
               ') UPDATE sites SET display_id = numbered.number FROM numbered WHERE sites.id = numbered.id')
    op.alter_column('sites', 'display_id', existing_type=sa.BigInteger(), nullable=False)
    op.execute('ALTER TABLE sites ALTER COLUMN display_id '
               'ADD GENERATED ALWAYS AS IDENTITY (START WITH 1 MINVALUE 1 NO CYCLE)')
    op.execute("SELECT setval(pg_get_serial_sequence('sites', 'display_id'), "
               "COALESCE((SELECT MAX(display_id) FROM sites), 1), EXISTS(SELECT 1 FROM sites))")
    op.create_unique_constraint('uq_sites_display_id', 'sites', ['display_id'])
    op.create_check_constraint('ck_site_display_id_positive', 'sites', 'display_id > 0')


def downgrade():
    op.drop_constraint('ck_site_display_id_positive', 'sites', type_='check')
    op.drop_constraint('uq_sites_display_id', 'sites', type_='unique')
    # PostgreSQL drops the owned identity sequence together with this column.
    op.drop_column('sites', 'display_id')
