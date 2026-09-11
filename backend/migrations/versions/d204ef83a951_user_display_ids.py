"""Assign persistent numeric user labels without changing UUID ownership keys."""
import sqlalchemy as sa
from alembic import op

revision = 'd204ef83a951'
down_revision = 'b713a6c42d90'
branch_labels = None
depends_on = None


def upgrade():
    # ALTER holds the table lock until migration commit, so new accounts cannot
    # race the stable backfill or receive a number before the sequence is set.
    op.add_column('users', sa.Column('display_id', sa.BigInteger(), nullable=True))
    op.execute('WITH numbered AS ('
               'SELECT id, row_number() OVER (ORDER BY created_at, id) AS number FROM users'
               ') UPDATE users SET display_id = numbered.number FROM numbered WHERE users.id = numbered.id')
    op.alter_column('users', 'display_id', existing_type=sa.BigInteger(), nullable=False)
    op.execute('ALTER TABLE users ALTER COLUMN display_id '
               'ADD GENERATED ALWAYS AS IDENTITY (START WITH 1 MINVALUE 1 NO CYCLE)')
    op.execute("SELECT setval(pg_get_serial_sequence('users', 'display_id'), "
               "COALESCE((SELECT MAX(display_id) FROM users), 1), EXISTS(SELECT 1 FROM users))")
    op.create_unique_constraint('uq_users_display_id', 'users', ['display_id'])
    op.create_check_constraint('ck_user_display_id_positive', 'users', 'display_id > 0')


def downgrade():
    op.drop_constraint('ck_user_display_id_positive', 'users', type_='check')
    op.drop_constraint('uq_users_display_id', 'users', type_='unique')
    # PostgreSQL drops the column-owned identity sequence with the column.
    op.drop_column('users', 'display_id')
