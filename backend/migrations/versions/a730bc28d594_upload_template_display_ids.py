"""Give receiving templates persistent numeric labels while retaining UUID history."""
import sqlalchemy as sa
from alembic import op

revision = 'a730bc28d594'
down_revision = 'f629a17e4b82'
branch_labels = None
depends_on = None


def upgrade():
    # ALTER locks the table through commit, making numbering and identity
    # initialization atomic with respect to concurrent template creation.
    op.add_column('site_upload_templates', sa.Column('display_id', sa.BigInteger(), nullable=True))
    op.execute('WITH numbered AS ('
               'SELECT id, row_number() OVER (ORDER BY created_at, id) AS number FROM site_upload_templates'
               ') UPDATE site_upload_templates SET display_id = numbered.number FROM numbered '
               'WHERE site_upload_templates.id = numbered.id')
    op.alter_column('site_upload_templates', 'display_id', existing_type=sa.BigInteger(), nullable=False)
    op.execute('ALTER TABLE site_upload_templates ALTER COLUMN display_id '
               'ADD GENERATED ALWAYS AS IDENTITY (START WITH 1 MINVALUE 1 NO CYCLE)')
    op.execute("SELECT setval(pg_get_serial_sequence('site_upload_templates', 'display_id'), "
               "COALESCE((SELECT MAX(display_id) FROM site_upload_templates), 1), "
               "EXISTS(SELECT 1 FROM site_upload_templates))")
    op.create_unique_constraint('uq_upload_templates_display_id', 'site_upload_templates', ['display_id'])
    op.create_check_constraint('ck_upload_template_display_id_positive', 'site_upload_templates', 'display_id > 0')


def downgrade():
    op.drop_constraint('ck_upload_template_display_id_positive', 'site_upload_templates', type_='check')
    op.drop_constraint('uq_upload_templates_display_id', 'site_upload_templates', type_='unique')
    # The owned PostgreSQL identity sequence is dropped with its column.
    op.drop_column('site_upload_templates', 'display_id')
