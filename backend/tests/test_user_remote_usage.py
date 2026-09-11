"""Account consumption is the observed NewAPI total, independent of settlement facts."""
from copy import deepcopy
from decimal import Decimal

import pytest
from app.db import engine, uid
from app.models import Site
from app.models_channels import Distribution, Task, TaskItem
from sqlalchemy import event, select
from test_remote_usage_totals import distribution, manual_proof
from test_settlement_orders import _create, _observe
from test_settlement_orders import order_case as _order_fixture
from test_settlement_orders import service_case as _service_fixture

order_case = _order_fixture
service_case = _service_fixture


def _users(api, **params):
    response = api.get('/api/users', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(data):
    return {row['id']: row for row in data['items']}


def _assert_total(row, amount, covered, total):
    assert row['remote_usage_total'] == {
        'amount': amount, 'unit': 'USD', 'covered': covered, 'total': total}


def test_accounts_without_remote_channels_show_zero_and_keep_visibility(db, users, login):
    root = _by_id(_users(login('root')))
    assert set(root) == {account.id for name, account in users.items() if name != 'root'}
    for row in root.values():
        _assert_total(row, '0', 0, 0)
    admin = _by_id(_users(login('admin')))
    assert set(admin) == {users['user'].id, users['sibling'].id}
    assert login('user').get('/api/users').status_code == 403


def test_remote_total_includes_admin_and_direct_children_but_isolates_other_accounts(
        db, users, order_case, login):
    # The fixture gives the first child 100 USD. Every owner has a distinct target.
    order_case.make(owner='admin', quota=2_500_000)
    order_case.make(owner='sibling', quota=1_000_000)
    order_case.make(owner='other_admin', quota=7_000_000)
    order_case.make(owner='other_user', quota=9_000_000)
    db.commit()
    root = _by_id(_users(login('root')))
    _assert_total(root[users['admin'].id], '107', 3, 3)
    _assert_total(root[users['user'].id], '100', 1, 1)
    _assert_total(root[users['sibling'].id], '2', 1, 1)
    _assert_total(root[users['other_admin'].id], '32', 2, 2)
    _assert_total(root[users['other_user'].id], '18', 1, 1)
    admin = _by_id(_users(login('admin')))
    assert set(admin) == {users['user'].id, users['sibling'].id}
    assert admin[users['user'].id]['remote_usage_total'] == root[users['user'].id]['remote_usage_total']


def test_current_remote_counters_change_without_facts_and_orders_do_not_add_or_subtract_usage(
        db, users, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')
    first = _by_id(_users(api))[users['user'].id]
    _assert_total(first, '100', 1, 1)
    assert first['settlement_order_count'] == 0
    order = _create(api, [channel])
    assert Decimal(order['payment_amount']) == 80
    _assert_total(_by_id(_users(api))[users['user'].id], '100', 1, 1)
    order_case.fact(order_case.pair, '9999')
    _observe(dist, 62_500_000)
    db.commit()
    updated = _by_id(_users(api))[users['user'].id]
    _assert_total(updated, '125', 1, 1)
    assert updated['settlement_order_count'] == 1
    assert Decimal(updated['settlement_order_amount']) == 80


def test_same_remote_id_on_distinct_sites_contributes_both_amounts_once(db, users, order_case, login):
    channel, first = order_case.pair
    site = db.get(Site, first.site_id)
    second_site = Site(id=uid(), name='Second remote quota fixture', prefix='quota-second',
        base_url='https://quota-second.invalid', seller_user_id='1',
        token_encrypted=site.token_encrypted, adapter=site.adapter,
        verified_at=site.verified_at, capabilities=deepcopy(site.capabilities))
    db.add(second_site)
    db.flush()
    second = Distribution(id=uid(), channel_id=channel.id, site_id=second_site.id,
        remote_id=first.remote_id, remote_name='same-id-on-another-site', key_count=50)
    _observe(second, 12_500_000)
    db.add(second)
    db.commit()
    # A target contributes once regardless of how many credential keys it holds.
    _assert_total(_by_id(_users(login('root')))[users['user'].id], '125', 2, 2)


def test_archived_channel_usage_counts_but_deleted_tombstone_and_unassigned_targets_do_not(
        db, users, order_case, login):
    channel, _ = order_case.pair
    channel.archived = True
    _, deleted = order_case.make(quota=2_500_000)
    deleted.status = 'deleted'
    _, tombstone = order_case.make(quota=3_500_000)
    tombstone.remote_snapshot = {**tombstone.remote_snapshot,
        '_local_deletion': {'remote_confirmed': False}}
    _, unassigned = order_case.make(quota=4_500_000)
    unassigned.remote_id = None
    db.commit()
    rows = _by_id(_users(login('root')))
    _assert_total(rows[users['user'].id], '100', 1, 1)
    _assert_total(rows[users['admin'].id], '100', 1, 1)


@pytest.mark.parametrize('missing', ['counter', 'conversion', 'identity', 'state'])
def test_existing_targets_without_reliable_amount_are_unknown_not_zero(
        db, users, order_case, login, missing):
    _, dist = order_case.pair
    if missing == 'counter':
        dist.remote_snapshot = {'id': dist.remote_id, 'status': 3}
    elif missing == 'conversion':
        db.get(Site, dist.site_id).capabilities = {}
    elif missing == 'identity':
        dist.remote_snapshot = {**dist.remote_snapshot, 'id': 'previous-remote-target'}
    else:
        dist.status = 'failed'
    db.commit()
    _assert_total(_by_id(_users(login('root')))[users['user'].id], None, 0, 1)


def test_partial_coverage_keeps_known_zero_distinct_from_missing_observations(db, users, order_case, login):
    _, known = order_case.pair
    _observe(known, 0)
    _, missing = order_case.make()
    missing.remote_snapshot = {'id': missing.remote_id, 'status': 3}
    db.commit()
    _assert_total(_by_id(_users(login('root')))[users['user'].id], '0', 1, 2)


def test_large_and_fractional_remote_usage_is_serialized_without_float_rounding(db, users, order_case, login):
    _, dist = order_case.pair
    _observe(dist, 9_007_199_254_740_993)
    order_case.make(quota=1)
    db.commit()
    rows = _by_id(_users(login('root')))
    _assert_total(rows[users['user'].id], '18014398509.481988', 2, 2)
    _assert_total(rows[users['admin'].id], '18014398509.481988', 2, 2)


def test_account_total_uses_matching_manual_sync_completion_evidence(db, users, order_case, login):
    channel, dist = order_case.pair
    task = Task(id=uid(), actor_id=users['admin'].id, owner_id=users['user'].id,
        actor_session_version=users['admin'].session_version, kind='channel_action', status='succeeded')
    db.add(task)
    db.flush()
    row = distribution(dist.remote_id, site_id=dist.site_id, quota=1_250_000)
    row.id = dist.id
    row.remote_snapshot['_monitoring']['usage_sync']['task_id'] = task.id
    proof = next(iter(manual_proof(row).values()))
    dist.remote_snapshot, dist.last_sync_at = row.remote_snapshot, row.last_sync_at
    item = TaskItem(id=uid(), task_id=task.id, channel_id=channel.id, site_id=dist.site_id,
        distribution_id=dist.id, operation='sync_usage', status='succeeded',
        updated_at=proof['updated_at'],
        snapshot={'operation_target': proof['target'], 'operation_result': proof['result']})
    db.add(item)
    db.commit()
    api = login('root')
    _assert_total(_by_id(_users(api))[users['user'].id], '2.5', 1, 1)
    item.snapshot = {**item.snapshot, 'operation_target': {'id': 'different-target'}}
    db.commit()
    _assert_total(_by_id(_users(api))[users['user'].id], None, 0, 1)


def test_paginated_totals_are_stable_read_only_and_have_bounded_queries(db, users, order_case, login):
    for owner in ('admin', 'sibling', 'other_admin', 'other_user'):
        order_case.make(owner=owner)
    db.commit()
    api = login('root')
    before = {row.id: deepcopy(row.remote_snapshot) for row in db.scalars(select(Distribution))}

    def listing(**params):
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().lower())

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            data = _users(api, **params)
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
        return data, len(statements)

    first, one_count = listing(limit=1)
    all_users, all_count = listing()
    assert all_count <= one_count + 1
    assert first['items'][0] == _by_id(all_users)[first['items'][0]['id']]
    assert first['total'] == all_users['total'] == 5
    empty, _ = listing(offset=5)
    assert empty == {'items': [], 'total': 5}
    assert {row.id: row.remote_snapshot for row in db.scalars(
        select(Distribution).execution_options(populate_existing=True))} == before
    for secret in ('fixture-private-key', 'fixture-site-token', 'fixture-order-snapshot-secret',
                   'remote_snapshot', 'token_encrypted', 'password_hash'):
        assert secret not in str(all_users)
