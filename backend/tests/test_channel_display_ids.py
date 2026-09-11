"""Persistent channel numbers label UUID identities without changing ownership or history."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db import SessionLocal, engine
from app.models import AuditEvent, Category, CredentialFormat, Site
from app.models_billing import UsageFact
from app.models_channels import (
    Channel,
    Distribution,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from app.security import encrypt
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def catalog(db, users):
    category = Category(name='OpenAI', family='OpenAI')
    site = Site(name='Number fixture', prefix='fixture', base_url='https://fixture.invalid',
                seller_user_id='71', token_encrypted=encrypt('fixture-site-token'))
    db.add_all([category, site])
    db.flush()
    fmt = CredentialFormat(category_id=category.id, code='api_key-v1', name='Fixture API key',
                           schema_config={'type': 'api_key', 'remote_type': 1})
    db.add(fmt)
    db.flush()
    bindings = {}
    for owner, user in users.items():
        group = UploadGroup(owner_id=user.id, category_id=category.id, format_id=fmt.id, tag='fixture-' + owner)
        db.add(group)
        db.flush()
        bindings[owner] = {'owner_id': user.id, 'group_id': group.id,
                           'category_id': category.id, 'format_id': fmt.id}
    db.commit()
    return bindings, site.id


def channel(catalog, label, *, owner='user', **values):
    return Channel(**catalog[0][owner], key_encrypted=encrypt('fixture-private-key'),
                   key_hint='masked', fingerprint='fixture-' + label, **values)


def add_distribution(db, catalog, row, **values):
    dist = Distribution(channel_id=row.id, site_id=catalog[1], remote_name='Fixture remote name', **values)
    db.add(dist)
    db.flush()
    return dist


def migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'f8a24d09b6c1_channel_display_ids.py'
    spec = importlib.util.spec_from_file_location('channel_display_ids_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'e849c25a6d10'
    return module


def history_snapshot(db):
    tables = ('users', 'sites', 'upload_groups', 'key_versions', 'distributions', 'tasks', 'task_items',
              'usage_facts', 'audit_events')
    return {table: [dict(row) for row in db.execute(text(f'SELECT * FROM {table} ORDER BY id')).mappings()]
            for table in tables}


@pytest.mark.parametrize('populated', [False, True])
def test_migration_backfills_active_archived_and_empty_channels_preserving_uuid_history(db, catalog, users, populated):
    assert engine.dialect.name == 'postgresql' and 'test' in engine.url.database.lower()
    if populated:
        for suffix, month, archived in [(9, 1, False), (7, 3, False), (3, 2, False), (1, 2, True)]:
            row = channel(catalog, str(suffix), id=f'00000000-0000-0000-0000-{suffix:012d}',
                          created_at=datetime(2025, month, 1, tzinfo=UTC).replace(tzinfo=None), archived=archived,
                          models=['fixture-model'], remark='Original note', upload_settings={'original': True})
            db.add(row)
            db.flush()
            dist = add_distribution(db, catalog, row, remote_id=str(suffix),
                                    remote_snapshot={'channel_id': row.id, 'name': 'Original remote name'})
            task = Task(actor_id=row.owner_id, owner_id=row.owner_id, actor_session_version=1,
                        kind='create', snapshot={'channel_ids': [row.id]})
            db.add(task)
            db.flush()
            db.add_all([
                KeyVersion(channel_id=row.id, version=1, key_encrypted=row.key_encrypted, fingerprint=row.fingerprint),
                TaskItem(task_id=task.id, channel_id=row.id, site_id=dist.site_id, distribution_id=dist.id,
                         operation='create', snapshot={'channel_id': row.id, 'remote_id': dist.remote_id}),
                UsageFact(source_id='fixture-' + str(suffix), site_id=dist.site_id, distribution_id=dist.id,
                          channel_id=row.id, owner_id=row.owner_id, admin_id=users['admin'].id,
                          category_id=row.category_id, occurred_at=row.created_at, raw_amount=8, raw_unit='USD',
                          amount=8, unit='USD', conversion_version='fixture', verified=True),
                AuditEvent(actor_id=row.owner_id, action='fixture.original', object_type='channel', object_id=row.id,
                           site_id=dist.site_id, summary={'channel_id': row.id, 'unchanged': True}),
            ])
        db.flush()
    module = migration(db)
    module.downgrade()
    assert 'display_id' not in {column['name'] for column in inspect(db.connection()).get_columns('channels')}
    before = [dict(row) for row in db.execute(text('SELECT * FROM channels ORDER BY created_at, id')).mappings()]
    original_history = history_snapshot(db)
    for attempt in range(2):
        module.upgrade()
        after = [dict(row) for row in db.execute(text('SELECT * FROM channels ORDER BY created_at, id')).mappings()]
        assert [row['display_id'] for row in after] == list(range(1, len(before) + 1))
        assert [{key: value for key, value in row.items() if key != 'display_id'} for row in after] == before
        assert history_snapshot(db) == original_history
        if attempt == 0:
            module.downgrade()
    fresh = channel(catalog, 'after-migration')
    db.add(fresh)
    db.commit()
    assert fresh.display_id == len(before) + 1
    assigned = dict(db.execute(select(Channel.id, Channel.display_id)).all())
    db.close()
    with SessionLocal() as reopened:
        assert dict(reopened.execute(select(Channel.id, Channel.display_id)).all()) == assigned


def test_database_generates_positive_unique_nonnullable_numbers_and_rejects_assignments(db, catalog):
    inspector = inspect(db.connection())
    column = next(value for value in inspector.get_columns('channels') if value['name'] == 'display_id')
    assert column['nullable'] is False
    assert column['identity']['always'] is True and column['identity']['start'] == 1
    assert column['identity']['minvalue'] == 1 and column['identity']['cycle'] is False
    assert any(value['column_names'] == ['display_id'] for value in inspector.get_unique_constraints('channels'))
    assert any(value['name'] == 'ck_channel_display_id_positive' for value in inspector.get_check_constraints('channels'))
    for number in (0, -1, 999):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.add(channel(catalog, 'forged-' + str(number), display_id=number))
            db.flush()
    stored = channel(catalog, 'stored')
    db.add(stored)
    db.flush()
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(text('UPDATE channels SET display_id = 900 WHERE id = :id'), {'id': stored.id})


def test_concurrent_numbers_are_unique_and_deleted_or_rolled_back_numbers_are_not_reused(db, catalog):
    barrier = Barrier(6)

    def create(index):
        with SessionLocal() as session:
            row = channel(catalog, 'parallel-' + str(index))
            session.add(row)
            barrier.wait(timeout=10)
            session.commit()
            return row.id, row.display_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(create, range(6)))
    assert len({number for _, number in created}) == 6
    last_id, last_number = max(created, key=lambda pair: pair[1])
    db.delete(db.get(Channel, last_id))
    db.commit()
    discarded = channel(catalog, 'discarded')
    db.add(discarded)
    db.flush()
    discarded_number = discarded.display_id
    db.rollback()
    replacement = channel(catalog, 'replacement')
    db.add(replacement)
    db.commit()
    assert replacement.display_id > discarded_number > last_number > 0
    assert dict(db.execute(select(Channel.id, Channel.display_id).where(Channel.id != replacement.id)).all()) == {
        id_: number for id_, number in created if id_ != last_id}


def listing(client, path='/api/channels', **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert 'fixture-private-key' not in response.text and 'fixture-site-token' not in response.text
    return response.json()


def test_list_detail_distribution_pagination_and_filters_keep_assigned_numbers(db, catalog, login):
    rows = []
    for index in range(5):
        row = channel(catalog, 'list-' + str(index), id=f'00000000-0000-0000-0000-{index + 1:012d}',
                      created_at=datetime(2025, 1, 1, tzinfo=UTC).replace(tzinfo=None), archived=index == 3,
                      owner='other_user' if index == 4 else 'user', models=['fixture-model'])
        db.add(row)
        db.flush()
        add_distribution(db, catalog, row)
        rows.append(row)
    db.commit()
    client = login('user')
    expected = {row.id: row.display_id for row in rows[:3]}
    for params in ({}, {'model': 'fixture-model'}, {'category_id': rows[0].category_id, 'site_id': catalog[1]}):
        first, second = listing(client, limit=2, **params), listing(client, limit=2, offset=2, **params)
        assert first['total'] == second['total'] == 3
        paged = first['items'] + second['items']
        assert [row['id'] for row in paged] == [row.id for row in reversed(rows[:3])]
        assert {row['id']: row['display_id'] for row in paged} == expected
    for row in rows[:4]:
        response = client.get('/api/channels/' + row.id)
        assert response.status_code == 200
        assert response.json()['display_id'] == row.display_id and type(response.json()['display_id']) is int
    archived = listing(client, archived=True)['items']
    assert [(row['id'], row['display_id']) for row in archived] == [(rows[3].id, rows[3].display_id)]
    distributions = listing(client, '/api/channel-distributions', site_id=catalog[1])['items']
    assert {row['channel']['id']: row['channel']['display_id'] for row in distributions} == expected
    assert all(row['channel_id'] == row['channel']['id'] for row in distributions)
    original = rows[0]
    changed = client.patch('/api/channels/' + original.id, json={'remark': 'Changed note'})
    assert changed.status_code == 200 and changed.json()['display_id'] == original.display_id
    assert client.get('/api/channels/' + str(original.display_id)).status_code == 404
    assert client.patch('/api/channels/' + str(original.display_id), json={'remark': 'Wrong identity'}).status_code == 404


@pytest.mark.parametrize(('actor', 'allowed'), [('root', {'user', 'other_user'}), ('admin', {'user'}), ('user', {'user'})])
def test_numeric_search_retains_uuid_and_owner_permissions(db, catalog, login, users, actor, allowed):
    # A distinctive sequence value avoids accidental matches in UUIDs and fixture labels.
    db.execute(text("SELECT setval(pg_get_serial_sequence('channels', 'display_id'), 123456, false)"))
    rows = {}
    for owner, letter in [('user', 'a'), ('other_user', 'b')]:
        row = channel(catalog, 'search-' + owner, owner=owner,
                      id=f'{letter * 8}-{letter * 4}-{letter * 4}-{letter * 4}-{letter * 12}')
        db.add(row)
        db.flush()
        add_distribution(db, catalog, row)
        rows[owner] = row
    db.commit()
    client = login(actor)
    for path in ('/api/channels', '/api/channel-distributions'):
        items = listing(client, path, search='12345')['items']
        summaries = [row['channel'] for row in items] if path.endswith('distributions') else items
        assert {row['id']: row['display_id'] for row in summaries} == {
            rows[owner].id: rows[owner].display_id for owner in allowed}
        assert listing(client, path, search='12345%') == {'items': [], 'total': 0}
        for owner, row in rows.items():
            for search in (str(row.display_id), row.id):
                assert listing(client, path, search=search)['total'] == int(owner in allowed)
    for owner, row in rows.items():
        assert client.get('/api/channels/' + row.id).status_code == (200 if owner in allowed else 404)
        if owner not in allowed:
            assert client.patch('/api/channels/' + row.id, json={'remark': 'Forbidden'}).status_code == 404
            assert client.get('/api/channels', params={'owner_id': users[owner].id,
                              'search': str(row.display_id)}).status_code == 404


@pytest.mark.parametrize('number', [1, 999, '9', None])
def test_clients_cannot_supply_or_modify_channel_numbers(db, catalog, login, number):
    row = channel(catalog, 'existing')
    db.add(row)
    db.commit()
    original_number = row.display_id
    client = login('user')
    body = {'category_id': row.category_id, 'format_id': row.format_id, 'credentials': 'fixture-private-key',
            'idempotency_key': 'fixture-display-number', 'display_id': number}
    responses = [client.post('/api/uploads/' + endpoint, json=body) for endpoint in ('submit', 'simple-submit')]
    responses.append(client.patch('/api/channels/' + row.id, json={'display_id': number}))
    for response in responses:
        assert response.status_code == 422, response.text
        assert any(error['loc'] == ['body', 'display_id'] and error['type'] == 'extra_forbidden'
                   for error in response.json()['detail'])
    db.expire_all()
    assert db.get(Channel, row.id).display_id == original_number
    assert list(db.scalars(select(Channel.id))) == [row.id]
