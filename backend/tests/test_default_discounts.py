"""Zero defaults start at account creation without rewriting existing discount history."""
from datetime import timedelta
from decimal import Decimal

import pytest
from app.billing_services import scoped_discount
from app.bootstrap import initialize, seed_catalog
from app.db import SessionLocal, engine, uid, utcnow
from app.default_discounts import ensure_user_default_discounts
from app.models import AuditEvent, Category, Site, User
from app.models_billing import DiscountVersion, SettlementOrder, SettlementOrderLine
from app.routers import users as users_router
from sqlalchemy import event, func, select
from test_channel_categories import service_case as _service_fixture
from test_remote_usage_totals import STAMP, VERSION, conversion
from test_settlement_orders import _create, _observe

service_case = _service_fixture


@pytest.fixture
def discount_catalog(db, users):
    seed_catalog(db)
    db.commit()
    return {row.family: row for row in db.scalars(select(Category))}


def _rates(db, payee_id, category_id=None):
    query = select(DiscountVersion).where(DiscountVersion.payee_id == payee_id)
    if category_id is not None:
        query = query.where(DiscountVersion.category_id == category_id)
    return list(db.scalars(query.order_by(DiscountVersion.effective_at, DiscountVersion.created_at, DiscountVersion.id)))


def _snapshot(db):
    return {row.id: (row.payer_id, row.payee_id, row.layer, row.category_id, row.service_variant,
                    row.inherits_category, row.percent, row.effective_at, row.created_at)
            for row in db.scalars(select(DiscountVersion))}


def _assert_defaults(rows, user, payer_id, layer, category_ids):
    assert len(rows) == len(category_ids)
    assert {row.category_id for row in rows} == category_ids
    for row in rows:
        assert (row.payer_id, row.payee_id, row.layer) == (payer_id, user.id, layer)
        assert row.service_variant is None and row.inherits_category is False
        assert row.percent == Decimal(0) and row.effective_at == user.created_at


def _new_rate(db, user, category, at, percent, *, variant=None):
    row = DiscountVersion(id=uid(), payer_id=user.parent_id, payee_id=user.id, layer='lower',
        category_id=category.id, service_variant=variant, percent=Decimal(percent), effective_at=at)
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize(('role', 'layer'), [('admin', 'upper'), ('user', 'lower')])
def test_api_creation_commits_creation_time_zero_defaults_for_both_root_child_roles(
        db, users, discount_catalog, login, role, layer):
    result = login('root').post('/api/users', json={'username': f'new-default-{role}',
        'nickname': 'Default fixture account', 'password': 'test-password-123!', 'role': role})
    assert result.status_code == 201, result.text
    account_id = result.json()['id']
    with SessionLocal() as observer:
        account = observer.get(User, account_id)
        assert account.role == role and account.parent_id == users['root'].id
        _assert_defaults(_rates(observer, account_id), account, users['root'].id, layer,
                         {category.id for category in discount_catalog.values()})
    # Creating one account does not opportunistically backfill unrelated existing users.
    assert db.scalar(select(func.count(DiscountVersion.id))) == len(discount_catalog)


def test_api_account_and_its_defaults_roll_back_together_when_creation_fails(db, users, discount_catalog, login,
                                                                           monkeypatch):
    api = login('root')
    before_ids = set(db.scalars(select(User.id)))

    def fail_after_defaults(session, actor, action, object_type, object_id, summary):
        assert action == 'user.create'
        pending_user = session.get(User, object_id)
        _assert_defaults(_rates(session, object_id), pending_user, actor.id, 'upper',
                         {category.id for category in discount_catalog.values()})
        raise RuntimeError('fixture failure before creation commit')

    monkeypatch.setattr(users_router, 'audit', fail_after_defaults)
    with pytest.raises(RuntimeError, match='fixture failure before creation commit'):
        api.post('/api/users', json={'username': 'rollback-defaults', 'nickname': 'Rollback fixture',
            'password': 'test-password-123!', 'role': 'admin'})
    assert set(db.scalars(select(User.id))) == before_ids
    assert db.scalar(select(func.count(DiscountVersion.id))) == 0


def test_initialize_backfills_legacy_relationships_once_but_seed_catalog_does_not(db, users, discount_catalog):
    assert db.scalar(select(func.count(DiscountVersion.id))) == 0
    seed_catalog(db)
    db.commit()
    assert db.scalar(select(func.count(DiscountVersion.id))) == 0
    assert initialize(db).id == users['root'].id
    category_ids = {category.id for category in discount_catalog.values()}
    for name in ('admin', 'other_admin', 'user', 'sibling', 'other_user'):
        account = users[name]
        payer_id = users['root'].id if account.role == 'admin' else account.parent_id
        layer = 'upper' if account.role == 'admin' else 'lower'
        _assert_defaults(_rates(db, account.id), account, payer_id, layer, category_ids)
    assert _rates(db, users['root'].id) == []
    original = _snapshot(db)
    initialize(db)
    assert _snapshot(db) == original


def test_default_helper_is_idempotent_inside_the_callers_uncommitted_transaction(db, users, discount_catalog):
    assert ensure_user_default_discounts(db, users['user'].id) == len(discount_catalog)
    initial = _snapshot(db)
    assert ensure_user_default_discounts(db, users['user'].id) == 0
    assert _snapshot(db) == initial
    db.rollback()
    assert db.scalar(select(func.count(DiscountVersion.id))) == 0


@pytest.mark.parametrize('seconds', [-1, 0], ids=['earlier-than-creation', 'at-creation'])
def test_existing_broad_rate_at_or_before_creation_is_never_overwritten(db, users, discount_catalog, seconds):
    account = users['user']
    account.created_at = utcnow()-timedelta(days=40)
    category = discount_catalog['AWS']
    original = _new_rate(db, account, category, account.created_at+timedelta(seconds=seconds), '35')
    db.commit()
    before = _snapshot(db)[original.id]
    initialize(db)
    assert _snapshot(db)[original.id] == before
    assert [row.id for row in _rates(db, account.id, category.id)] == [original.id]
    assert len(_rates(db, account.id)) == len(discount_catalog)
    history = _rates(db, account.id, category.id)
    assert scoped_discount(history, account.created_at).id == original.id


def test_later_positive_and_future_versions_survive_with_zero_filling_the_earlier_interval(
        db, users, discount_catalog):
    account = users['user']
    account.created_at = utcnow()-timedelta(days=40)
    category = discount_catalog['AWS']
    later = _new_rate(db, account, category, account.created_at+timedelta(days=10), '35')
    future = _new_rate(db, account, category, utcnow()+timedelta(days=2), '55')
    db.commit()
    before = _snapshot(db)
    initialize(db)
    assert all(_snapshot(db)[key] == value for key, value in before.items())
    history = _rates(db, account.id, category.id)
    assert len(history) == 3
    baseline = history[0]
    assert baseline.percent == 0 and baseline.effective_at == account.created_at
    assert scoped_discount(history, account.created_at-timedelta(microseconds=1)) is None
    assert scoped_discount(history, account.created_at).id == baseline.id
    assert scoped_discount(history, later.effective_at-timedelta(microseconds=1)).id == baseline.id
    assert scoped_discount(history, later.effective_at).id == later.id
    assert scoped_discount(history, utcnow()).id == later.id
    assert scoped_discount(history, future.effective_at).id == future.id
    completed = _snapshot(db)
    initialize(db)
    assert _snapshot(db) == completed


def test_existing_service_override_stays_above_new_broad_zero_default(db, users, discount_catalog, login):
    account = users['user']
    account.created_at = utcnow()-timedelta(days=40)
    category = discount_catalog['AWS']
    fine = _new_rate(db, account, category, account.created_at-timedelta(days=1), '25', variant='bedrock')
    db.commit()
    original = _snapshot(db)[fine.id]
    initialize(db)
    assert _snapshot(db)[fine.id] == original
    history = _rates(db, account.id, category.id)
    assert scoped_discount(history, account.created_at, 'bedrock').id == fine.id
    assert scoped_discount(history, account.created_at, 'aws_claude').percent == 0
    result = login('admin').get('/api/discounts', params={'payee_id': account.id})
    assert result.status_code == 200, result.text
    services = {row['service_variant']: row for row in result.json()['services'] if row['category_id'] == category.id}
    assert services['bedrock']['current']['id'] == fine.id and not services['bedrock']['inherited']
    assert services['aws_claude']['inherited']
    assert Decimal(services['aws_claude']['current']['percent']) == 0


def test_discount_get_is_read_only_and_all_eleven_services_inherit_initialized_zero(db, users, discount_catalog, login):
    api = login('admin')
    path = '/api/discounts'
    params = {'payee_id': users['user'].id}
    before = api.get(path, params=params)
    assert before.status_code == 200, before.text
    assert len(before.json()['services']) == 11
    assert all(row['current'] is None for row in before.json()['services'])
    assert db.scalar(select(func.count(DiscountVersion.id))) == 0
    initialize(db)
    expected = _snapshot(db)
    audit_count = db.scalar(select(func.count(AuditEvent.id)))
    statements = []

    def record_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', record_sql)
    try:
        response = api.get(path, params=params)
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, 'before_cursor_execute', record_sql)
    assert not any(statement.startswith(('insert ', 'update ', 'delete ')) for statement in statements)
    services = response.json()['services']
    assert len(services) == 11
    for row in services:
        assert row['inherited'] is True and row['override'] is None
        assert Decimal(row['current']['percent']) == 0
        assert row['current']['id'] == row['default_current']['id']
    assert _snapshot(db) == expected
    assert db.scalar(select(func.count(AuditEvent.id))) == audit_count


def test_default_zero_is_frozen_on_new_order_without_repricing_usage(db, users, service_case, login):
    initialize(db)
    channel, dist = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    site = db.get(Site, dist.site_id)
    site.adapter = 'tcp-red-v1'
    site.verified_at = STAMP.replace(tzinfo=None)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(500000)}
    _observe(dist, 50_000_000)
    db.commit()
    order = _create(login('admin'), [channel])
    assert order['usage_amount'] == '100' and order['payment_amount'] == '0'
    assert order['lines'][0]['discount_percent'] == '0'
    assert db.scalar(select(func.count(SettlementOrder.id))) == 1
    assert db.scalar(select(func.count(SettlementOrderLine.id))) == 1
