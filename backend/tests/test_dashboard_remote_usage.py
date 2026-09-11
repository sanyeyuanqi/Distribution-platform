"""Dashboard consumption sums authorized remote counters without counting teams twice."""
from copy import deepcopy
from decimal import Decimal

import pytest
from app.db import engine, uid
from app.models import Site
from app.models_channels import Distribution, Task, TaskItem
from sqlalchemy import event, select, text
from test_remote_usage_totals import conversion, distribution, manual_proof
from test_settlement_orders import _observe
from test_settlement_orders import order_case as _order_fixture
from test_settlement_orders import service_case as _service_fixture

order_case = _order_fixture
service_case = _service_fixture


def _dashboard(api, **params):
    response = api.get('/api/dashboard', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _assert_total(data, amount, covered, total):
    assert data['remote_usage_total'] == {
        'amount': amount, 'unit': 'USD', 'covered': covered, 'total': total}


def test_empty_remote_scope_is_zero_for_every_role(login):
    for role in ('root', 'admin', 'user'):
        _assert_total(_dashboard(login(role)), '0', 0, 0)


def test_all_users_are_summed_once_and_scope_filters_stay_authorized(db, users, order_case, login):
    # Child: 100; admin: 5; sibling: 2; other team: 14 + 18; root: 3.
    for owner, quota in [('admin', 2_500_000), ('sibling', 1_000_000),
                         ('other_admin', 7_000_000), ('other_user', 9_000_000),
                         ('root', 1_500_000)]:
        order_case.make(owner=owner, quota=quota)
    db.commit()
    root, admin, child = login('root'), login('admin'), login('user')
    _assert_total(_dashboard(root), '142', 6, 6)
    _assert_total(_dashboard(admin), '107', 3, 3)
    _assert_total(_dashboard(child), '100', 1, 1)
    _assert_total(_dashboard(root, scope='mine'), '3', 1, 1)
    _assert_total(_dashboard(admin, scope='mine'), '5', 1, 1)
    _assert_total(_dashboard(root, owner_id=users['admin'].id), '5', 1, 1)
    _assert_total(_dashboard(admin, owner_id=users['user'].id), '100', 1, 1)
    for api in (admin, child):
        assert api.get('/api/dashboard', params={'owner_id': users['other_user'].id}).status_code == 404
    assert root.get('/api/dashboard', params={'scope': 'all'}).status_code == 422


def test_same_remote_target_across_owners_is_counted_once_and_conflicts_are_unknown(
        db, order_case, login):
    # Simulate duplicate legacy observations before the global target constraint.
    # The disposable test schema is recreated by the db fixture for every test.
    db.execute(text('ALTER TABLE distributions DROP CONSTRAINT uq_distribution_site_remote'))
    _, first = order_case.pair
    _, duplicate = order_case.make(owner='other_user')
    duplicate.remote_id = first.remote_id
    _observe(duplicate, 50_000_000)
    db.commit()
    root = login('root')
    _assert_total(_dashboard(root), '100', 1, 1)
    _observe(duplicate, 12_500_000)
    db.commit()
    _assert_total(_dashboard(root), None, 0, 1)
    _assert_total(_dashboard(login('user')), '100', 1, 1)


def test_same_remote_id_at_two_sites_uses_each_sites_conversion(db, order_case, login):
    channel, first = order_case.pair
    site = db.get(Site, first.site_id)
    second_site = Site(id=uid(), name='Other dashboard counter site', prefix='dash-second',
        base_url='https://dashboard-second.invalid', seller_user_id='1',
        token_encrypted=site.token_encrypted, adapter=site.adapter,
        verified_at=site.verified_at,
        capabilities={**site.capabilities, 'usage_conversion': conversion(1_000_000)})
    db.add(second_site)
    db.flush()
    second = Distribution(id=uid(), channel_id=channel.id, site_id=second_site.id,
        remote_id=first.remote_id, remote_name='same-id-second-site', key_count=50)
    _observe(second, 12_500_000)
    db.add(second)
    db.commit()
    _assert_total(_dashboard(login('root')), '112.5', 2, 2)


def test_archived_channels_count_but_deleted_tombstoned_and_unassigned_targets_do_not(
        db, order_case, login):
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
    _assert_total(_dashboard(login('root')), '100', 1, 1)


@pytest.mark.parametrize('missing', ['counter', 'conversion', 'identity', 'state'])
def test_unreliable_existing_targets_stay_unknown(db, order_case, login, missing):
    _, dist = order_case.pair
    if missing == 'counter':
        dist.remote_snapshot = {'id': dist.remote_id, 'status': 3}
    elif missing == 'conversion':
        db.get(Site, dist.site_id).capabilities = {}
    elif missing == 'identity':
        dist.remote_snapshot = {**dist.remote_snapshot, 'id': 'previous-target'}
    else:
        dist.status = 'failed'
    db.commit()
    _assert_total(_dashboard(login('root')), None, 0, 1)


def test_partial_coverage_keeps_known_zero_separate_from_unobserved_targets(db, order_case, login):
    _, known = order_case.pair
    _observe(known, 0)
    _, missing = order_case.make(owner='sibling')
    missing.remote_snapshot = {'id': missing.remote_id, 'status': 3}
    db.commit()
    _assert_total(_dashboard(login('root')), '0', 1, 2)


def test_counter_amounts_keep_decimal_precision_and_legacy_fact_summaries(db, order_case, login):
    _, dist = order_case.pair
    _observe(dist, 9_007_199_254_740_993)
    order_case.make(quota=1)
    order_case.fact(order_case.pair, '9999')
    order_case.fact(order_case.pair, '7', verified=False)
    db.commit()
    data = _dashboard(login('root'))
    _assert_total(data, '18014398509.481988', 2, 2)
    assert Decimal(data['usage_by_unit']['USD']) == 9999
    assert Decimal(data['known_usage_by_unit']['USD']) == 10006
    assert data['coverage'] == {'covered': 1, 'total': 2}
    assert data['unverified_count'] == 1
    assert Decimal(data['categories'][0]['totals_by_unit']['USD']) == 9999
    assert sum(Decimal(day['totals_by_unit'].get('USD', '0')) for day in data['trend']) == 9999


def test_manual_sync_amount_requires_matching_completion_evidence(db, users, order_case, login):
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
    _assert_total(_dashboard(api), '2.5', 1, 1)
    item.snapshot = {**item.snapshot, 'operation_target': {'id': 'other-target'}}
    db.commit()
    _assert_total(_dashboard(api), None, 0, 1)


def test_dashboard_is_read_only_and_never_loads_site_tokens(db, order_case, login):
    api = login('root')
    before = {row.id: deepcopy(row.remote_snapshot) for row in db.scalars(select(Distribution))}
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        data = _dashboard(api)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert not any('token_encrypted' in sql for sql in statements)
    assert {row.id: row.remote_snapshot for row in db.scalars(
        select(Distribution).execution_options(populate_existing=True))} == before
    for secret in ('fixture-private-key', 'fixture-site-token', 'fixture-order-snapshot-secret',
                   'remote_snapshot', 'token_encrypted', 'password_hash'):
        assert secret not in str(data)
