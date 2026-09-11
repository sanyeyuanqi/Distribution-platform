"""Database-assigned account labels never replace UUID authorization identities."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.auth import user_json
from app.db import Base, SessionLocal, engine
from app.models import AuditEvent, User
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError


def migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'd204ef83a951_user_display_ids.py'
    spec = importlib.util.spec_from_file_location('user_display_ids_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'b713a6c42d90'
    return module


def account(username, **values):
    return User(username=username, nickname=username, password_hash='mock-hash', role='user', **values)


def first_day(year, month):
    return datetime(year, month, 1, tzinfo=UTC).replace(tzinfo=None)


@pytest.mark.parametrize('populated', [False, True])
def test_upgrade_backfills_all_legacy_users_stably_and_preserves_uuid_fields(db, populated):
    module = migration(db)
    module.downgrade()
    legacy = []
    if populated:
        for suffix, role, created, archived, parent in [
            (9, 'superadmin', first_day(2024, 1), False, None),
            (3, 'admin', first_day(2025, 1), False, 9),
            (7, 'user', first_day(2025, 2), False, 3),
            (1, 'user', first_day(2025, 1), True, 3),
        ]:
            legacy.append({'id': f'00000000-0000-0000-0000-{suffix:012d}', 'username': f'legacy-{suffix}',
                'nickname': f'Original {suffix}', 'password_hash': f'unchanged-hash-{suffix}', 'role': role,
                'parent_id': f'00000000-0000-0000-0000-{parent:012d}' if parent else None,
                'active': not archived, 'archived': archived, 'session_version': suffix, 'created_at': created})
        db.execute(text('INSERT INTO users (id, username, nickname, password_hash, role, parent_id, active, archived, session_version, created_at) '
            'VALUES (:id, :username, :nickname, :password_hash, :role, :parent_id, :active, :archived, :session_version, :created_at)'), legacy)
        db.add(AuditEvent(actor_id=legacy[0]['id'], actor_role='superadmin', action='fixture.original',
            object_type='user', object_id=legacy[2]['id'], summary={'user_id': legacy[2]['id'], 'unchanged': True}))
        db.flush()
    before_audit = [dict(row) for row in db.execute(text('SELECT * FROM audit_events')).mappings()]
    module.upgrade()
    after = [dict(row) for row in db.execute(text('SELECT * FROM users ORDER BY created_at, id')).mappings()]
    assert [row['display_id'] for row in after] == list(range(1, len(legacy) + 1))
    assert [{key: value for key, value in row.items() if key != 'display_id'} for row in after] == sorted(legacy, key=lambda row: (row['created_at'], row['id']))
    assert [dict(row) for row in db.execute(text('SELECT * FROM audit_events')).mappings()] == before_audit
    fresh = account('after-migration')
    db.add(fresh)
    db.commit()
    assert fresh.display_id == len(legacy) + 1
    assigned = dict(db.execute(select(User.id, User.display_id)).all())
    db.close()
    with SessionLocal() as reopened:
        assert dict(reopened.execute(select(User.id, User.display_id)).all()) == assigned


def test_clean_alembic_upgrade_matches_user_metadata_and_repeated_head_does_not_renumber(db):
    assert engine.dialect.name == 'postgresql' and 'test' in engine.url.database.lower()
    db.close()
    Base.metadata.drop_all(engine)
    config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).parents[1] / 'migrations'))
    try:
        command.upgrade(config, 'head')
        value = account('fresh-install')
        db.add(value)
        db.commit()
        assert value.display_id == 1
        before = user_json(value)
        db.close()
        command.upgrade(config, 'head')
        context = MigrationContext.configure(db.connection(), opts={
            'include_object': lambda obj, name, type_, reflected, compare_to: type_ != 'table' or name == 'users'})
        assert compare_metadata(context, Base.metadata) == []
        assert user_json(db.get(User, before['id'])) == before
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == ScriptDirectory.from_config(config).get_current_head()
    finally:
        db.rollback()
        with engine.begin() as connection:
            connection.execute(text('DROP TABLE IF EXISTS alembic_version'))


def test_database_identity_is_always_positive_unique_nonnullable_and_noncycling(db):
    column = next(column for column in inspect(db.connection()).get_columns('users') if column['name'] == 'display_id')
    assert column['nullable'] is False
    assert column['identity']['always'] is True and column['identity']['start'] == 1
    assert column['identity']['minvalue'] == 1 and column['identity']['cycle'] is False
    assert any(value['column_names'] == ['display_id'] for value in inspect(db.connection()).get_unique_constraints('users'))
    assert any(value['name'] == 'ck_user_display_id_positive' for value in inspect(db.connection()).get_check_constraints('users'))
    for value in (0, -1, 999):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.add(account('explicit-' + str(value), display_id=value))
            db.flush()


def test_concurrent_accounts_get_distinct_numbers_and_deleted_or_rolled_back_numbers_are_not_reused(db):
    barrier = Barrier(6)
    def create(index):
        with SessionLocal() as session:
            value = account(f'parallel-{index}')
            session.add(value)
            barrier.wait(timeout=10)
            session.commit()
            return value.id, value.display_id
    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(create, range(6)))
    numbers = [number for _, number in created]
    assert len(set(numbers)) == 6 and min(numbers) > 0
    last_id, last_number = max(created, key=lambda pair: pair[1])
    db.delete(db.get(User, last_id))
    db.commit()
    discarded = account('discarded-number')
    db.add(discarded)
    db.flush()
    discarded_number = discarded.display_id
    db.rollback()
    replacement = account('after-discard')
    db.add(replacement)
    db.commit()
    assert replacement.display_id > discarded_number > last_number


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_api_creation_lists_and_auth_expose_stable_display_ids_without_changing_uuid_scope(db, login, users, role):
    client = login('root')
    me = client.get('/api/auth/me').json()['user']
    assert me['display_id'] == users['root'].display_id and me['id'] == users['root'].id
    payload = {'username': 'display-created', 'nickname': 'Display account', 'password': 'test-password-123!', 'role': role}
    response = client.post('/api/users', json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    assert type(created['display_id']) is int and created['display_id'] > max(row.display_id for row in users.values())
    assert len(created['id']) == 36 and created['parent_id'] == users['root'].id
    assert client.patch('/api/users/' + str(created['display_id']), json={'nickname': 'Must not resolve'}).status_code == 404
    renamed = client.patch('/api/users/' + created['id'], json={'nickname': 'Renamed'})
    assert renamed.status_code == 200 and renamed.json()['display_id'] == created['display_id']
    listed = next(row for row in client.get('/api/users').json()['items'] if row['id'] == created['id'])
    assert listed['display_id'] == created['display_id']
    archived = client.delete('/api/users/' + created['id'])
    assert archived.status_code == 200 and archived.json()['display_id'] == created['display_id']
    assert next(row for row in client.get('/api/users?include_archived=true').json()['items'] if row['id'] == created['id'])['display_id'] == created['display_id']
    db.expire_all()
    assert user_json(db.get(User, created['id']))['display_id'] == created['display_id']


@pytest.mark.parametrize('value', [1, 999, '9', None])
def test_user_display_id_is_not_client_assignable_or_editable(login, users, value):
    client = login('root')
    response = client.post('/api/users', json={'username': 'forged-display', 'nickname': 'No',
        'password': 'test-password-123!', 'display_id': value})
    assert response.status_code == 422
    assert client.patch('/api/users/' + users['admin'].id, json={'display_id': value}).status_code == 422


def test_numeric_labels_do_not_expand_role_or_cross_parent_access(login, users):
    admin = login('admin')
    response = admin.get('/api/users')
    assert {row['display_id'] for row in response.json()['items']} == {users['user'].display_id, users['sibling'].display_id}
    assert admin.patch('/api/users/' + users['other_user'].id, json={'nickname': 'Forbidden'}).status_code == 404
    assert admin.patch('/api/users/' + str(users['other_user'].display_id), json={'nickname': 'Forbidden'}).status_code == 404
    assert login('user').get('/api/users').status_code == 403
