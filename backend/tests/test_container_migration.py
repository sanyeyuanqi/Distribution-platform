"""Run the additive migration on the disposable database without reading secrets."""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db import uid
from app.models import Category, CredentialFormat, Site
from app.models_channels import (
    Channel,
    Distribution,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from app.security import encrypt, fingerprint


def test_migration_preserves_all_legacy_credential_and_snapshot_values(db, users):
    category = Category(id=uid(), name='OpenAI', family='OpenAI')
    db.add(category)
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=category.id, code='api_key-v1', name='API Key', version='1',
                           schema_config={'type': 'api_key', 'remote_type': 1}, default_models=[])
    site = Site(id=uid(), name='Fixture', prefix='fixture', base_url='https://fixture.invalid', seller_user_id='1',
                token_encrypted=encrypt('fixture-token'), adapter='new-api-v1')
    db.add_all([fmt, site])
    db.flush()
    group = UploadGroup(id=uid(), owner_id=users['user'].id, category_id=category.id, format_id=fmt.id, tag='immutable-group')
    db.add(group)
    db.flush()
    channel = Channel(id=uid(), owner_id=users['user'].id, group_id=group.id, category_id=category.id, format_id=fmt.id,
        key_encrypted=encrypt('never-reencrypt-me'), key_hint='masked', fingerprint=fingerprint('never-reencrypt-me'), models=['fixture'])
    db.add(channel)
    db.flush()
    dist = Distribution(id=uid(), channel_id=channel.id, site_id=site.id, remote_id='123', remote_name='old-remote',
                        template_snapshot={'legacy': ['unchanged']}, remote_snapshot={'id': 123})
    version = KeyVersion(channel_id=channel.id, version=1, key_encrypted=channel.key_encrypted, fingerprint=channel.fingerprint)
    task = Task(id=uid(), actor_id=users['user'].id, owner_id=users['user'].id,
                actor_session_version=1, kind='upload', snapshot={'old': True})
    db.add_all([dist, version, task])
    db.flush()
    item = TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id, operation='create',
                    snapshot={'format_schema': {'type': 'api_key', 'remote_type': 1}, 'old_task': 'unchanged'})
    db.add(item)
    db.commit()
    before = (channel.id, channel.key_encrypted, channel.fingerprint, version.key_encrypted, dict(item.snapshot), dict(dist.template_snapshot))
    connection = db.connection()
    ops = Operations(MigrationContext.configure(connection))
    ops.drop_table('channel_credentials')
    ops.drop_constraint('uq_distribution_channel_site_partition', 'distributions', type_='unique')
    ops.create_unique_constraint('uq_distribution_channel_site', 'distributions', ['channel_id', 'site_id'])
    for column in ('partition_key', 'partition_label', 'key_count'):
        ops.drop_column('distributions', column)
    ops.drop_column('channels', 'key_mode')
    ops.drop_column('channels', 'key_count')
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'b713a6c42d90_credential_containers.py'
    spec = importlib.util.spec_from_file_location('container_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = ops
    module.upgrade()
    db.expire_all()
    assert (channel.id, channel.key_encrypted, channel.fingerprint, version.key_encrypted, item.snapshot, dist.template_snapshot) == before
    assert channel.key_mode == 'single' and channel.key_count == 1 and dist.partition_key == '' and dist.key_count == 1
    member = connection.execute(sa.text('SELECT owner_id, channel_id, ordinal, fingerprint FROM channel_credentials')).one()
    assert tuple(member) == (channel.owner_id, channel.id, 0, channel.fingerprint)
    db.commit()
