"""Store per-account daily dismissal independently of announcement read status."""
import sqlalchemy as sa
from alembic import op

revision = 'e508cb6a721f'
down_revision = 'd204ef83a951'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('announcement_reads', sa.Column('dismissed_on', sa.Date(), nullable=True))
    op.alter_column('announcement_reads', 'read_at', existing_type=sa.DateTime(), nullable=True)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM announcement_reads WHERE read_at IS NULL LIMIT 1')).first():
        raise RuntimeError('Cannot downgrade while unread daily dismissal records exist')
    op.drop_column('announcement_reads', 'dismissed_on')
    op.alter_column('announcement_reads', 'read_at', existing_type=sa.DateTime(), nullable=False)
