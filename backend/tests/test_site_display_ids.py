"""Numeric site labels are persistent; UUIDs still identify all operations and history."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.db import Base, SessionLocal, engine
from app.models import (
    AuditEvent,
    Category,
    CredentialFormat,
    Site,
    SiteUploadTemplate,
    User,
)
from app.models_channels import (
    Channel,
    Distribution,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)
from app.routers import sites
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError


def site(label, **values):
    return Site(name=label, prefix=label, base_url=f'https://{label}.invalid',
        seller_user_id='71', token_encrypted='fixture-ciphertext', **values)


def migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'f629a17e4b82_site_display_ids.py'
    spec = importlib.util.spec_from_file_location('site_display_ids_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'e508cb6a721f'
    return module


def add_history(db, rows):
    owner = User(username='historical-owner', password_hash='mock-hash', role='user')
    category = Category(name='OpenAI', family='OpenAI')
    db.add_all([owner, category])
    db.flush()
    fmt = CredentialFormat(category_id=category.id, code='legacy-key', name='Original format')
    db.add(fmt)
    db.flush()
    group = UploadGroup(owner_id=owner.id, category_id=category.id, format_id=fmt.id, tag='legacy-batch')
    db.add(group)
    db.flush()
    channel = Channel(owner_id=owner.id, group_id=group.id, category_id=category.id, format_id=fmt.id,
        key_encrypted='unchanged-fixture-ciphertext', key_hint='masked', fingerprint='fixture-fingerprint')
    task = Task(actor_id=owner.id, owner_id=owner.id, actor_session_version=1, kind='create',
        snapshot={'site_ids': [row.id for row in rows]})
    db.add_all([channel, task])
    db.flush()
    for row in rows:
        distribution = Distribution(channel_id=channel.id, site_id=row.id, remote_id='42',
            remote_name='Original remote name', remote_snapshot={'site_id': row.id, 'unchanged': True})
        db.add(distribution)
        db.flush()
        db.add_all([
            SiteUploadTemplate(site_id=row.id, category_id=category.id, format_id=fmt.id, name='Original template'),
            AuditEvent(actor_id=owner.id, action='fixture.original', object_type='site', object_id=row.id,
                site_id=row.id, summary={'site_id': row.id, 'unchanged': True}),
            TaskItem(task_id=task.id, channel_id=channel.id, site_id=row.id, distribution_id=distribution.id,
                operation='create', snapshot={'site_id': row.id, 'remote_name': 'Original remote name'}),
            UnclaimedChannel(site_id=row.id, remote_id='88', remote_name='Original unclaimed'),
        ])
    db.flush()


def history_snapshot(db):
    tables = ('users', 'categories', 'credential_formats', 'site_upload_templates', 'upload_groups',
        'channels', 'distributions', 'tasks', 'task_items', 'unclaimed_channels', 'audit_events')
    return {table: [dict(row) for row in db.execute(text(f'SELECT * FROM {table} ORDER BY id')).mappings()]
        for table in tables}


@pytest.mark.parametrize('populated', [False, True])
def test_migration_backfills_active_and_archived_sites_stably_preserving_all_history(db, populated):
    if populated:
        # Insertion order differs from created_at order, and equal timestamps use UUID order.
        rows = [site(f'legacy-{suffix}', id=f'00000000-0000-0000-0000-{suffix:012d}',
                     created_at=datetime(2025, month, 1, tzinfo=UTC).replace(tzinfo=None), archived=archived,
                     capabilities={'fixture': suffix}, routing_group='普通', enabled=not archived)
                for suffix, month, archived in [(9, 1, False), (7, 3, False), (3, 2, False), (1, 2, True)]]
        db.add_all(rows)
        db.flush()
        add_history(db, rows)
    module = migration(db)
    module.downgrade()
    assert 'display_id' not in {column['name'] for column in inspect(db.connection()).get_columns('sites')}
    before = [dict(row) for row in db.execute(text('SELECT * FROM sites ORDER BY created_at, id')).mappings()]
    original_history = history_snapshot(db)
    for _ in range(2):
        module.upgrade()
        after = [dict(row) for row in db.execute(text('SELECT * FROM sites ORDER BY created_at, id')).mappings()]
        assert [row['display_id'] for row in after] == list(range(1, len(before) + 1))
        assert [{key: value for key, value in row.items() if key != 'display_id'} for row in after] == before
        assert history_snapshot(db) == original_history
        if _ == 0:
            module.downgrade()
    new = site('after-migration')
    db.add(new)
    db.commit()
    assert new.display_id == len(before) + 1
    assigned = dict(db.execute(select(Site.id, Site.display_id)).all())
    db.close()
    with SessionLocal() as reopened:
        assert dict(reopened.execute(select(Site.id, Site.display_id)).all()) == assigned


def test_clean_alembic_upgrade_and_downgrade_match_site_metadata(db):
    assert engine.dialect.name == 'postgresql' and 'test' in engine.url.database.lower()
    db.close()
    Base.metadata.drop_all(engine)
    config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).parents[1] / 'migrations'))
    current_head = ScriptDirectory.from_config(config).get_current_head()
    try:
        command.upgrade(config, 'head')
        value = site('fresh-install')
        db.add(value)
        db.commit()
        assert value.display_id == 1
        original_id = value.id
        db.close()
        command.upgrade(config, 'head')
        command.downgrade(config, 'e508cb6a721f')
        command.upgrade(config, 'head')
        context = MigrationContext.configure(db.connection(), opts={
            'include_object': lambda obj, name, type_, reflected, compare_to: type_ != 'table' or name == 'sites'})
        assert compare_metadata(context, Base.metadata) == []
        assert db.get(Site, original_id).display_id == 1
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == current_head
    finally:
        db.rollback()
        with engine.begin() as connection:
            connection.execute(text('DROP TABLE IF EXISTS alembic_version'))


def test_site_identity_is_always_positive_unique_nonnullable_and_noncycling(db):
    inspector = inspect(db.connection())
    column = next(column for column in inspector.get_columns('sites') if column['name'] == 'display_id')
    assert column['nullable'] is False
    assert column['identity']['always'] is True and column['identity']['start'] == 1
    assert column['identity']['minvalue'] == 1 and column['identity']['cycle'] is False
    assert any(value['column_names'] == ['display_id'] for value in inspector.get_unique_constraints('sites'))
    assert any(value['name'] == 'ck_site_display_id_positive' for value in inspector.get_check_constraints('sites'))
    for value in (0, -1, 999):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.add(site('explicit-' + str(value), display_id=value))
            db.flush()
    original = site('original')
    db.add(original)
    db.flush()
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text('UPDATE sites SET display_id = 900 WHERE id = :id'), {'id': original.id})


def test_concurrent_sites_deleted_and_rolled_back_numbers_are_not_reused_or_reordered(db):
    barrier = Barrier(6)
    def create(index):
        with SessionLocal() as session:
            value = site(f'parallel-{index}')
            session.add(value)
            barrier.wait(timeout=10)
            session.commit()
            return value.id, value.display_id
    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(create, range(6)))
    assert len({number for _, number in created}) == 6
    last_id, last_number = max(created, key=lambda pair: pair[1])
    db.delete(db.get(Site, last_id))
    db.commit()
    discarded = site('discarded')
    db.add(discarded)
    db.flush()
    discarded_number = discarded.display_id
    db.rollback()
    replacement = site('replacement')
    db.add(replacement)
    db.commit()
    assert replacement.display_id > discarded_number > last_number > 0
    assert dict(db.execute(select(Site.id, Site.display_id).where(Site.id != replacement.id)).all()) == {
        id_: number for id_, number in created if id_ != last_id}


@pytest.fixture
def mock_site_api(monkeypatch):
    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    monkeypatch.setattr(sites, 'get_adapter', lambda row: SimpleNamespace(verify=lambda: {
        'verified_version': 'v0.13.2', 'capabilities': {'read': 'supported', 'create': 'supported', 'can_write': True}}))


def test_site_crud_responses_preserve_number_and_uuid_routes(login, db, mock_site_api):
    root = login('root')
    response = root.post('/api/sites', json={'name': 'Created', 'prefix': 'created',
        'base_url': 'https://fixture.invalid', 'seller_user_id': '71', 'token': 'fixture-token'})
    assert response.status_code == 201, response.text
    value = response.json()
    assert type(value['display_id']) is int and value['display_id'] == 1 and len(value['id']) == 36
    path = '/api/sites/' + value['id']
    assert root.patch('/api/sites/' + str(value['display_id']), json={'name': 'Wrong identity'}).status_code == 404
    updated = root.patch(path, json={'name': 'Renamed'})
    assert updated.status_code == 200 and updated.json()['display_id'] == value['display_id']
    assert root.post(path + '/verify').json()['display_id'] == value['display_id']
    assert root.get('/api/sites').json()['items'][0]['display_id'] == value['display_id']
    archived = root.request('DELETE', path, json={'confirmation': 'Renamed'})
    assert archived.status_code == 200 and archived.json()['display_id'] == value['display_id']
    db.expire_all()
    stored = db.get(Site, value['id'])
    assert stored.archived and stored.display_id == value['display_id']
    assert root.get('/api/sites').json()['items'] == []


@pytest.mark.parametrize('actor', ['root', 'admin', 'user'])
def test_display_numbers_do_not_expand_site_visibility_or_write_permissions(db, login, actor):
    values = [site('active', enabled=True), site('stopped', enabled=False), site('archived', archived=True)]
    db.add_all(values)
    db.commit()
    client = login(actor)
    rows = client.get('/api/sites').json()['items']
    expected = values[:2] if actor == 'root' else values[:1]
    assert {(row['id'], row['display_id']) for row in rows} == {(row.id, row.display_id) for row in expected}
    if actor != 'root':
        assert set(rows[0]) == {'id', 'display_id', 'name', 'health', 'enabled'}
        assert client.patch('/api/sites/' + values[0].id, json={'name': 'Forbidden'}).status_code == 403
        assert client.post('/api/sites/' + values[0].id + '/verify').status_code == 403


@pytest.mark.parametrize('value', [1, 999, '9', None])
def test_site_number_cannot_be_supplied_or_modified_by_clients(db, login, mock_site_api, value):
    stored = site('existing')
    db.add(stored)
    db.commit()
    root = login('root')
    response = root.post('/api/sites', json={'name': 'Forged', 'prefix': 'forged',
        'base_url': 'https://forged.invalid', 'seller_user_id': '71', 'token': 'fixture-token', 'display_id': value})
    assert response.status_code == 422
    assert root.patch('/api/sites/' + stored.id, json={'display_id': value}).status_code == 422
    db.expire_all()
    assert db.get(Site, stored.id).display_id == 1


@pytest.mark.parametrize('actor', ['root', 'admin', 'user'])
def test_sites_list_sorts_persistent_numbers_ascending_without_renumbering_filtered_sites(db, login, actor):
    values = [site(f'ordered-{index}', enabled=index != 1, archived=index == 3,
                   created_at=datetime(2027 - index, 1, 1, tzinfo=UTC).replace(tzinfo=None)) for index in range(4)]
    db.add_all(values)
    db.commit()
    rows = login(actor).get('/api/sites').json()['items']
    expected = values[:3] if actor == 'root' else [values[0], values[2]]
    assert [(row['id'], row['display_id']) for row in rows] == [(row.id, row.display_id) for row in expected]
