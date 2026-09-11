"""User statistics count frozen settlement order headers within the viewer's payer scope."""
from copy import deepcopy
from decimal import Decimal

import pytest
from app.db import engine, uid
from app.models import AuditEvent, Site
from app.models_billing import SettlementOrder, SettlementOrderLine
from sqlalchemy import event, select
from test_remote_usage_totals import VERSION, conversion
from test_settlement_orders import (
    ORDERS,
    _create,
    _observe,
    _rate,
    _usage_snapshot,
)
from test_settlement_orders import order_case as _order_fixture
from test_settlement_orders import service_case as _service_fixture

order_case = _order_fixture
service_case = _service_fixture


def _users(api, **params):
    response = api.get('/api/users', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _orders(api, **params):
    response = api.get(ORDERS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(data):
    return {row['id']: row for row in data['items']}


def _assert_summary(row, count, amount):
    assert type(row['settlement_order_count']) is int
    assert row['settlement_order_count'] == count
    assert isinstance(row['settlement_order_amount'], str)
    assert Decimal(row['settlement_order_amount']) == Decimal(amount)
    assert row['settlement_order_unit'] == 'USDT'


def _order_snapshot(db):
    models = (SettlementOrder, SettlementOrderLine, AuditEvent)
    return {model.__tablename__: {row.id: deepcopy({column.key: getattr(row, column.key)
            for column in model.__table__.columns}) for row in db.scalars(
                select(model).execution_options(populate_existing=True))}
            for model in models}


def test_users_without_orders_have_explicit_zero_statistics_and_existing_visibility(db, users, login):
    root = _by_id(_users(login('root')))
    assert set(root) == {account.id for key, account in users.items() if key != 'root'}
    for row in root.values():
        _assert_summary(row, 0, '0')
        assert row['settlement_order_amount'] == '0'
    admin = _by_id(_users(login('admin')))
    assert set(admin) == {users['user'].id, users['sibling'].id}
    for row in admin.values():
        _assert_summary(row, 0, '0')
    assert login('user').get('/api/users').status_code == 403


def test_multiple_lines_count_as_one_order_while_incremental_and_zero_orders_each_count_once(
        db, users, order_case, login):
    first, dist = order_case.pair
    second, _ = order_case.make(quota=12_500_000)
    zero, _ = order_case.make(code='newapi-14-aws-claude-v1')
    db.commit()
    admin = login('admin')
    first_order = _create(admin, [first, second])
    assert first_order['line_count'] == 2 and Decimal(first_order['payment_amount']) == 100
    _assert_summary(_by_id(_users(admin))[users['user'].id], 1, '100')
    zero_order = _create(admin, [zero])
    assert Decimal(zero_order['payment_amount']) == 0
    _observe(dist, 62_500_000)
    db.commit()
    increment = _create(admin, [first])
    assert Decimal(increment['usage_amount']) == 25 and Decimal(increment['payment_amount']) == 20
    # Current counter/rate changes after ordering cannot reprice the frozen totals.
    _observe(dist, 100_000_000)
    _rate(db, users, order_case, '5')
    db.commit()
    _assert_summary(_by_id(_users(admin))[users['user'].id], 3, '120')
    _assert_summary(_by_id(_users(login('root')))[users['user'].id], 3, '120')


def test_root_counts_the_actual_payee_not_group_owners_or_the_admins_outgoing_payments(
        db, users, order_case, login):
    child, _ = order_case.pair
    own, _ = order_case.make(owner='admin')
    db.commit()
    lower = _create(login('admin'), [child])
    upper = _create(login('root'), [child, own])
    assert Decimal(lower['payment_amount']) == 80
    assert upper['line_count'] == 2 and Decimal(upper['payment_amount']) == 180
    root_api = login('root')
    root = _by_id(_users(root_api))
    _assert_summary(root[users['admin'].id], 1, '180')
    _assert_summary(root[users['user'].id], 1, '80')
    _assert_summary(root[users['other_admin'].id], 0, '0')
    for payee, expected in (('admin', upper), ('user', lower)):
        history = _orders(root_api, payee_id=users[payee].id)
        assert history['total'] == root[users[payee].id]['settlement_order_count']
        assert [row['id'] for row in history['items']] == [expected['id']]
        assert sum(Decimal(row['payment_amount']) for row in history['items']) == Decimal(
            root[users[payee].id]['settlement_order_amount'])
    # Existing owner/team filtering still includes both settlement layers.
    for account in ('admin', 'user'):
        related = _orders(root_api, account_id=users[account].id)
        assert related['total'] == 2
        assert {row['id'] for row in related['items']} == {upper['id'], lower['id']}
    combined = _orders(root_api, account_id=users['user'].id, payee_id=users['admin'].id)
    assert combined['total'] == 1 and combined['items'][0]['id'] == upper['id']
    assert _orders(root_api, account_id=users['other_user'].id, payee_id=users['admin'].id) == {
        'items': [], 'total': 0}
    admin = _by_id(_users(login('admin')))
    assert users['admin'].id not in admin and users['other_user'].id not in admin
    _assert_summary(admin[users['user'].id], 1, '80')
    _assert_summary(admin[users['sibling'].id], 0, '0')


def test_new_admin_cannot_see_prior_payer_amounts_after_user_moves_but_root_keeps_full_history(
        db, users, order_case, login):
    channel, _ = order_case.pair
    old_admin = login('admin')
    old_order = _create(old_admin, [channel])
    users['user'].parent_id = users['other_admin'].id
    _rate(db, users, order_case, '60', payer='other_admin')
    db.commit()
    new_admin = login('other_admin')
    _assert_summary(_by_id(_users(new_admin))[users['user'].id], 0, '0')
    assert _orders(new_admin, payee_id=users['user'].id) == {'items': [], 'total': 0}
    assert old_admin.get(ORDERS, params={'payee_id': users['user'].id}).status_code == 404
    assert [row['id'] for row in _orders(old_admin)['items']] == [old_order['id']]
    _assert_summary(_by_id(_users(login('root')))[users['user'].id], 1, '80')
    assert users['user'].id not in _by_id(_users(login('admin')))
    new_order = _create(new_admin, [channel])
    assert Decimal(new_order['payment_amount']) == 60
    _assert_summary(_by_id(_users(new_admin))[users['user'].id], 1, '60')
    _assert_summary(_by_id(_users(login('root')))[users['user'].id], 2, '140')
    scoped = _orders(new_admin, payee_id=users['user'].id)
    assert scoped['total'] == 1 and scoped['items'][0]['id'] == new_order['id']
    for actor in ('root', 'user'):
        history = _orders(login(actor), payee_id=users['user'].id)
        assert history['total'] == 2
        assert {row['id'] for row in history['items']} == {old_order['id'], new_order['id']}


def test_usage_facts_do_not_contribute_to_new_order_statistics(db, users, order_case, login):
    admin = login('admin')
    order_case.fact(order_case.pair, '1200')
    order_case.fact(order_case.pair, '500', verified=False)
    db.commit()
    original = _usage_snapshot(db)
    assert len(original['usage_facts']) == 2
    _assert_summary(_by_id(_users(admin))[users['user'].id], 0, '0')
    assert _orders(admin) == {'items': [], 'total': 0}
    fresh, _ = order_case.make(quota=5_000_000)
    db.commit()
    order = _create(admin, [fresh])
    row = _by_id(_users(admin))[users['user'].id]
    _assert_summary(row, 1, '8')
    assert Decimal(order['usage_amount']) == 10 and Decimal(order['payment_amount']) == 8
    assert _orders(admin)['total'] == 1
    assert _usage_snapshot(db) == original


def test_pagination_and_archived_user_statistics_are_read_only_and_expose_no_order_secrets(
        db, users, order_case, login):
    _create(login('admin'), [order_case.pair[0]])
    users['user'].archived = True
    db.commit()
    root = login('root')
    before = _order_snapshot(db), _usage_snapshot(db)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        active = _users(root)
        all_users = _users(root, include_archived=True)
        index = next(index for index, row in enumerate(all_users['items']) if row['id'] == users['user'].id)
        page = _users(root, include_archived=True, offset=index, limit=1)
        empty = _users(root, include_archived=True, offset=all_users['total'], limit=1)
        history = _orders(root, payee_id=users['user'].id)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert users['user'].id not in _by_id(active)
    _assert_summary(_by_id(all_users)[users['user'].id], 1, '80')
    assert page['items'] == [_by_id(all_users)[users['user'].id]] and page['total'] == all_users['total']
    assert empty == {'items': [], 'total': all_users['total']}
    assert history['total'] == 1 and history['items'][0]['payee_id'] == users['user'].id
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert (_order_snapshot(db), _usage_snapshot(db)) == before
    for marker in ('fixture-private-key', 'fixture-site-token', 'fixture-order-snapshot-secret',
                   'idempotency_key', 'identity_snapshot', 'site_amounts', 'password_hash'):
        assert marker not in str(all_users) and marker not in str(history)


def test_payee_filter_preserves_search_and_pagination_without_including_other_recipients(
        db, users, order_case, login):
    first, _ = order_case.pair
    second, _ = order_case.make(quota=12_500_000)
    sibling, _ = order_case.make(owner='sibling')
    db.commit()
    admin = login('admin')
    own_orders = [_create(admin, [channel]) for channel in (first, second)]
    foreign = _create(admin, [sibling])
    root = login('root')
    history = _orders(root, payee_id=users['user'].id)
    assert history['total'] == 2
    assert {row['id'] for row in history['items']} == {row['id'] for row in own_orders}
    for offset, expected in enumerate(history['items']):
        assert _orders(root, payee_id=users['user'].id, offset=offset, limit=1) == {
            'items': [expected], 'total': 2}
    assert _orders(root, payee_id=users['user'].id, offset=2, limit=1) == {'items': [], 'total': 2}
    match = _orders(root, payee_id=users['user'].id, search=own_orders[0]['number'])
    assert match['total'] == 1 and match['items'][0]['id'] == own_orders[0]['id']
    assert _orders(root, payee_id=users['user'].id, search=foreign['number']) == {'items': [], 'total': 0}


@pytest.mark.parametrize(('actor', 'payee', 'status'), [
    ('root', 'admin', 200), ('root', 'other_user', 200),
    ('admin', 'user', 200), ('user', 'user', 200),
    ('root', 'missing', 404), ('root', 'root', 404),
    ('admin', 'admin', 404), ('admin', 'other_admin', 404), ('admin', 'other_user', 404),
    ('user', 'sibling', 404), ('user', 'admin', 404),
])
def test_payee_filter_requires_an_account_in_the_existing_viewer_scope(db, users, login, actor, payee, status):
    payee_id = uid() if payee == 'missing' else users[payee].id
    response = login(actor).get(ORDERS, params={'payee_id': payee_id})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json() == {'items': [], 'total': 0}


@pytest.mark.parametrize('payee_id', ['', 'x' * 37])
def test_payee_filter_rejects_empty_or_oversized_identifiers(db, users, login, payee_id):
    assert login('root').get(ORDERS, params={'payee_id': payee_id}).status_code == 422


def test_two_large_frozen_order_amounts_are_summed_without_float_rounding(db, users, order_case, login):
    first, first_dist = order_case.pair
    second, second_dist = order_case.make()
    for dist in (first_dist, second_dist):
        _observe(dist, 9_007_199_254_740_993)
    site = db.get(Site, first_dist.site_id)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(1)}
    db.commit()
    admin = login('admin')
    for channel in (first, second):
        order = _create(admin, [channel])
        assert order['payment_amount'] == '7205759403792794.4'
    row = _by_id(_users(admin))[users['user'].id]
    _assert_summary(row, 2, '14411518807585588.8')
    assert row['settlement_order_amount'] == '14411518807585588.8'
