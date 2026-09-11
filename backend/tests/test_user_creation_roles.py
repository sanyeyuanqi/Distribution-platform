"""Only superadmins create accounts; new accounts belong directly to their creator."""
from datetime import timedelta
from decimal import Decimal

import pytest
from app.auth import scope_owner_ids
from app.billing import settlement_parties
from app.db import SessionLocal, uid, utcnow
from app.models import AuditEvent, User
from app.models_billing import UsageFact
from app.models_channels import Channel, Distribution, UploadGroup
from app.routers import users as users_router
from settlement_fixtures import ledger as _ledger_fixture
from sqlalchemy import func, select
from test_remote_usage_totals import STAMP, VERSION, conversion
from test_settlement_orders import _create, _observe

ledger = _ledger_fixture


def body(**values):
    return {'username': 'created-account', 'nickname': 'Created account', 'password': 'test-password-123!', **values}


@pytest.mark.parametrize('role', [None, 'admin', 'user'])
def test_root_creates_both_roles_without_parent_input_and_preserves_legacy_ownership(db, login, users, role):
    original = dict(db.execute(select(User.id, User.parent_id)).all())
    payload = body()
    if role is not None:
        payload['role'] = role
    response = login('root').post('/api/users', json=payload)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value['role'] == (role or 'admin') and value['parent_id'] == users['root'].id
    assert len(value['id']) == 36 and type(value['display_id']) is int
    assert value['display_id'] > max(user.display_id for user in users.values())
    assert 'password' not in value and 'password_hash' not in value
    event = db.scalar(select(AuditEvent).where(AuditEvent.object_id == value['id'], AuditEvent.action == 'user.create'))
    assert event.actor_id == users['root'].id
    assert event.summary == {'role': role or 'admin', 'parent_id': users['root'].id}
    assert {id_: parent for id_, parent in db.execute(select(User.id, User.parent_id)).all() if id_ in original} == original
    for admin in ('admin', 'other_admin'):
        client = login(admin)
        assert value['id'] not in {row['id'] for row in client.get('/api/users').json()['items']}
        assert client.patch('/api/users/' + value['id'], json={'nickname': 'Forbidden'}).status_code == 404
    if role == 'user':
        payer, payee, layer = settlement_parties(db, users['root'], value['id'])
        assert (payer.id, payee.id, layer) == (users['root'].id, value['id'], 'lower')


def test_creation_does_not_require_any_administrator_account(db, login, users):
    root = login('root')
    for role in ('user', 'admin'):
        for row in db.scalars(select(User).where(User.role == role)).all():
            db.delete(row)
        db.flush()
    db.commit()
    assert db.scalar(select(func.count()).select_from(User).where(User.role == 'admin')) == 0
    response = root.post('/api/users', json=body(role='user'))
    assert response.status_code == 201 and response.json()['parent_id'] == users['root'].id
    assert db.scalar(select(func.count()).select_from(User).where(User.role == 'admin')) == 0


@pytest.mark.parametrize('parent', [None, 'root', 'admin', 'unknown'])
def test_parent_id_is_not_an_accepted_creation_field(db, login, users, parent):
    value = users[parent].id if parent in users else parent
    response = login('root').post('/api/users', json=body(role='user', parent_id=value))
    assert response.status_code == 422
    assert db.scalar(select(User).where(User.username == 'created-account')) is None


@pytest.mark.parametrize('actor', ['root', 'admin', 'user'])
def test_administrator_candidate_endpoint_is_removed(login, actor):
    assert not any(route.path == '/users/administrators' for route in users_router.router.routes)
    assert login(actor).get('/api/users/administrators').status_code in (404, 405)


@pytest.mark.parametrize('actor', ['admin', 'user'])
@pytest.mark.parametrize('role', [None, 'user', 'admin', 'superadmin'])
def test_only_superadmin_can_create_accounts(db, login, users, actor, role):
    payload = body()
    if role is not None:
        payload['role'] = role
    before = db.scalar(select(func.count()).select_from(User))
    response = login(actor).post('/api/users', json=payload)
    assert response.status_code == 403, response.text
    assert db.scalar(select(func.count()).select_from(User)) == before
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'user.create')) is None


def test_root_cannot_create_another_superadmin(login):
    assert login('root').post('/api/users', json=body(role='superadmin')).status_code == 422


def test_admin_retains_management_of_legacy_direct_users_without_creation(login, users, db):
    admin = login('admin')
    path = '/api/users/' + users['user'].id
    original_number = users['user'].display_id
    assert users['user'].id in {row['id'] for row in admin.get('/api/users').json()['items']}
    assert admin.patch(path, json={'nickname': 'Managed existing user'}).status_code == 200
    assert admin.patch(path, json={'active': False}).json()['active'] is False
    assert admin.patch(path, json={'active': True}).json()['active'] is True
    assert admin.delete(path).json()['archived'] is True
    db.expire_all()
    assert users['user'].role == 'user' and users['user'].parent_id == users['admin'].id
    assert users['user'].display_id == original_number
    assert not users['sibling'].archived


@pytest.mark.parametrize('actor', ['root', 'admin'])
def test_edit_still_cannot_change_role_or_parent_and_duplicates_remain_conflicts(db, login, users, actor):
    client = login(actor)
    for change in ({'role': 'admin'}, {'role': 'user'}, {'parent_id': users['root'].id}):
        assert client.patch('/api/users/' + users['user'].id, json=change).status_code == 422
    assert login('root').post('/api/users', json=body(username=users['user'].username)).status_code == 409
    db.expire_all()
    assert users['user'].role == 'user' and users['user'].parent_id == users['admin'].id


@pytest.mark.parametrize('change', ['disabled', 'archived', 'password_reset', 'role_changed'])
def test_creation_rechecks_actor_after_authentication_before_writing(db, login, monkeypatch, change):
    original = users_router.lock_creation_actor
    def changed_actor(session, actor):
        with SessionLocal() as concurrent:
            creator = concurrent.get(User, actor.id)
            if change == 'disabled':
                creator.active = False
            elif change == 'archived':
                creator.archived = True
            elif change == 'role_changed':
                creator.role = 'admin'
            else:
                creator.session_version += 1
            concurrent.commit()
        return original(session, actor)
    monkeypatch.setattr(users_router, 'lock_creation_actor', changed_actor)
    response = login('root').post('/api/users', json=body(role='user'))
    assert response.status_code == 401
    assert db.scalar(select(User).where(User.username == 'created-account')) is None


def test_direct_root_user_usage_and_lower_settlement_are_isolated_from_legacy_admins(db, login, users, ledger, client):
    root = login('root')
    response = root.post('/api/users', json=body(role='user'))
    assert response.status_code == 201
    owner = db.get(User, response.json()['id'])
    assert scope_owner_ids(db, owner) == [owner.id]
    assert owner.id in scope_owner_ids(db, users['root'])
    assert owner.id not in scope_owner_ids(db, users['admin'])
    category = ledger['cats']['OpenAI']
    group = UploadGroup(owner_id=owner.id, category_id=category.id, format_id=category.id, tag=uid())
    db.add(group)
    db.flush()
    channel = Channel(owner_id=owner.id, group_id=group.id, category_id=category.id, format_id=category.id,
        key_encrypted='fixture-ciphertext', key_hint='masked', fingerprint=uid(), created_at=utcnow()-timedelta(days=2))
    db.add(channel)
    db.flush()
    distribution = Distribution(channel_id=channel.id, site_id=ledger['site'].id, remote_id='direct-root-fixture',
        remote_name='Direct root fixture', created_at=utcnow()-timedelta(days=1))
    db.add(distribution)
    db.commit()
    discount = root.post('/api/discounts', json={'payee_id': owner.id, 'category_id': category.id, 'percent': '80'})
    assert discount.status_code == 201, discount.text
    assert discount.json()['payer_id'] == users['root'].id and discount.json()['layer'] == 'lower'
    imported = root.post('/api/usage/import', json={'items': [{
        'distribution_id': distribution.id, 'source_id': 'direct-root-usage', 'occurred_at': utcnow().isoformat() + 'Z',
        'raw_amount': '12.50', 'raw_unit': 'USD', 'amount': '12.50', 'unit': 'USD',
        'conversion_version': 'fixture-v1', 'verified': True, 'evidence': 'Isolated fixture usage evidence'}]})
    assert imported.status_code == 200, imported.text
    fact_id = imported.json()['items'][0]['id']
    fact = db.get(UsageFact, fact_id)
    assert (fact.owner_id, fact.admin_id) == (owner.id, users['root'].id)
    login_response = client.post('/api/auth/login', json={'username': owner.username, 'password': body()['password']})
    assert login_response.status_code == 200
    assert [row['id'] for row in client.get('/api/usage').json()['items']] == [fact_id]
    assert root.get('/api/usage', params={'owner_id': owner.id}).json()['totals_by_unit'] == {'USD': '12.5'}
    site = ledger['site']
    site.adapter = 'tcp-red-v1'
    site.verified_at = STAMP.replace(tzinfo=None)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(500000)}
    _observe(distribution, 6_250_000)
    db.commit()
    for name in ('admin', 'other_admin'):
        admin = login(name)
        assert admin.get('/api/usage', params={'owner_id': owner.id}).status_code == 404
        assert fact_id not in {row['id'] for row in admin.get('/api/usage').json()['items']}
        assert admin.get('/api/settlements/groups', params={'account_id': owner.id}).status_code == 404
        assert admin.post('/api/settlement-orders/preview', json={'channel_ids': [channel.id]}).status_code == 404
    order = _create(root, [channel])
    assert (order['payer_id'], order['payee_id'], order['layer']) == (users['root'].id, owner.id, 'lower')
    assert Decimal(order['usage_amount']) == Decimal('12.5') and Decimal(order['payment_amount']) == Decimal(10)
    assert order['lines'][0]['channel_id'] == channel.id
    assert client.get('/api/settlement-orders/' + order['id']).status_code == 200
    assert login('admin').get('/api/settlement-orders/' + order['id']).status_code == 404
    assert db.scalar(select(User.parent_id).where(User.id == users['user'].id)) == users['admin'].id
