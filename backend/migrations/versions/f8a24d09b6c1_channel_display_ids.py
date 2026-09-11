"""Assign persistent numeric channel labels while preserving UUID history."""
import sqlalchemy as sa
from alembic import op

revision = 'f8a24d09b6c1'
down_revision = 'e849c25a6d10'
branch_labels = None
depends_on = None


def upgrade():
    # ALTER holds the table lock until commit, preventing inserts from racing
    # the stable backfill (including archived channels) and sequence initialization.
    op.add_column('channels', sa.Column('display_id', sa.BigInteger(), nullable=True))
    op.execute('WITH numbered AS ('
               'SELECT id, row_number() OVER (ORDER BY created_at, id) AS number FROM channels'
               ') UPDATE channels SET display_id = numbered.number FROM numbered WHERE channels.id = numbered.id')
    op.alter_column('channels', 'display_id', existing_type=sa.BigInteger(), nullable=False)
    op.execute('ALTER TABLE channels ALTER COLUMN display_id '
               'ADD GENERATED ALWAYS AS IDENTITY (START WITH 1 MINVALUE 1 NO CYCLE)')
    op.execute("SELECT setval(pg_get_serial_sequence('channels', 'display_id'), "
               "COALESCE((SELECT MAX(display_id) FROM channels), 1), EXISTS(SELECT 1 FROM channels))")
    op.create_unique_constraint('uq_channels_display_id', 'channels', ['display_id'])
    op.create_check_constraint('ck_channel_display_id_positive', 'channels', 'display_id > 0')


def downgrade():
    op.drop_constraint('ck_channel_display_id_positive', 'channels', type_='check')
    op.drop_constraint('uq_channels_display_id', 'channels', type_='unique')
    # PostgreSQL drops the owned identity sequence together with this column.
    op.drop_column('channels', 'display_id')
