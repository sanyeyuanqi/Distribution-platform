"""Current remote quota totals stay independent of settlement and old targets."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.channel_observation import extract_usage, quota_conversion
from app.db import engine
from app.models_billing import UsageFact
from app.models_channels import Distribution, Task, TaskItem
from app.remote_usage_totals import remote_usage_total
from sqlalchemy import event, func, select
from test_channel_monitoring import monitor_action, observed  # noqa: F401
from test_channels import delete_case, payload, setup_catalog  # noqa: F401

STAMP = datetime(2026, 9, 9, 12, tzinfo=UTC)
VERSION = 'v1.0.0-rc.32-colin'


def conversion(ratio=500000):
    return quota_conversion({'version': VERSION, 'quota_per_unit': ratio},
                            adapter_kind='tcp-red-v1', verified_version=VERSION)


def usage(quota=0, ratio=500000):
    return extract_usage({'used_quota': quota}, conversion=conversion(ratio))


def distribution(remote_id='1', *, quota=0, site_id='site', **changes):
    return SimpleNamespace(id=f'dist-{site_id}-{remote_id}', site_id=site_id, remote_id=remote_id,
        status='disabled', last_sync_at=STAMP.replace(tzinfo=None), remote_snapshot={
            'id': remote_id, 'status': 3, '_monitoring': {'usage_sync': {
                'task_id': 'sync-task', 'synced_at': (STAMP + timedelta(microseconds=1)).isoformat(),
                'remote_usage': usage(quota)}}}, **changes)


def total(*rows, evidence=None, sites=None):
    return remote_usage_total(rows, sites or {}, evidence=evidence)


def test_zero_partial_exact_decimal_sum_and_no_key_multiplier():
    zero = distribution(key_count=100)
    assert total(zero) == {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 1}
    big = distribution('2', quota=9007199254740993)
    tiny = distribution('3', quota=1)
    missing = distribution('4')
    missing.remote_snapshot.pop('_monitoring')
    before = deepcopy(big.remote_snapshot)
    assert total(zero, big, tiny, missing) == {
        'amount': '18014398509.481988', 'unit': 'USD', 'covered': 3, 'total': 4}
    assert big.remote_snapshot == before
    assert total() == {'amount': None, 'unit': 'USD', 'covered': 0, 'total': 0}


@pytest.mark.parametrize('state', ['failed', 'pending', 'running', 'needs_review', 'missing',
                                  'created_pending_verification'])
def test_uncertain_current_states_have_no_amount_even_with_old_observation(state):
    row = distribution(quota=500000)
    row.status = state
    assert total(row) == {'amount': None, 'unit': 'USD', 'covered': 0, 'total': 1}


@pytest.mark.parametrize('state', [None, True, False, '2', 0, 4, -1])
def test_raw_status_must_be_known_strict_integer(state):
    row = distribution()
    row.remote_snapshot['status'] = state
    assert total(row)['covered'] == 0


def test_removed_no_id_and_local_tombstone_are_excluded_but_identity_mismatch_is_missing():
    deleted, no_id, tombstone, stale = [distribution(str(i)) for i in range(1, 5)]
    deleted.status = 'deleted'
    no_id.remote_id = None
    tombstone.remote_snapshot['_local_deletion'] = {'remote_confirmed': False}
    stale.remote_snapshot['id'] = 'previous-generation'
    assert total(deleted, no_id, tombstone, stale) == {
        'amount': None, 'unit': 'USD', 'covered': 0, 'total': 1}


def test_duplicate_target_counts_once_different_site_counts_separately_and_conflict_is_unknown():
    first = distribution(quota=500000)
    duplicate = deepcopy(first)
    duplicate.id = 'another-projection'
    other_site = distribution(quota=1000000, site_id='other-site')
    assert total(first, duplicate, other_site)['amount'] == '3'
    assert total(first, duplicate, other_site)['total'] == 2
    duplicate.remote_snapshot['_monitoring']['usage_sync']['remote_usage'] = usage(1500000)
    assert total(first, duplicate, other_site) == {
        'amount': '2', 'unit': 'USD', 'covered': 1, 'total': 2}
    duplicate.remote_snapshot = {'id': '1', 'status': 2}
    assert total(first, duplicate)['covered'] == 0


@pytest.mark.parametrize('change', ['missing_ratio', 'invalid_ratio', 'negative', 'bool', 'missing_quota',
                                  'bad_time', 'naive_time', 'no_link_time', 'old_time'])
def test_invalid_or_unbound_observation_is_not_fabricated_as_zero(change):
    row = distribution()
    observation = row.remote_snapshot['_monitoring']['usage_sync']
    values = observation['remote_usage']
    if change == 'missing_ratio':
        values.pop('conversion')
    elif change == 'invalid_ratio':
        values['conversion']['quota_per_unit'] = '-1'
    elif change == 'negative':
        values['used_quota'] = -1
    elif change == 'bool':
        values['used_quota'] = True
    elif change == 'missing_quota':
        values.pop('used_quota')
    elif change == 'bad_time':
        observation['synced_at'] = 'not-a-timestamp'
    elif change == 'naive_time':
        observation['synced_at'] = '2026-09-09T12:00:01'
    elif change == 'no_link_time':
        row.last_sync_at = None
    else:
        row.last_sync_at += timedelta(seconds=1)
    # Today's verified ratio is not allowed to fill/reprice a legacy sample.
    site = SimpleNamespace(adapter='tcp-red-v1', verified_at=STAMP,
        capabilities={'verified_version': VERSION, 'usage_conversion': conversion(1)})
    assert total(row, sites={'site': site})['amount'] is None


def test_saved_conversion_is_revalidated_not_repriced_and_untrusted_amount_is_ignored():
    row = distribution(quota=500000)
    row.remote_snapshot['_monitoring']['usage_sync']['remote_usage']['used_amount'] = '999999'
    site = SimpleNamespace(adapter='tcp-red-v1', verified_at=STAMP,
        capabilities={'verified_version': VERSION, 'usage_conversion': conversion(1)})
    assert total(row, sites={'site': site})['amount'] == '1'
    # A directly linked raw counter may instead use current verified site metadata.
    row.remote_snapshot['used_quota'] = 0
    assert total(row, sites={'site': site})['amount'] == '0'
    assert total(row)['amount'] is None


def manual_proof(row):
    observation = row.remote_snapshot['_monitoring']['usage_sync']
    row.last_sync_at = STAMP.replace(tzinfo=None) + timedelta(microseconds=2)
    return {(row.id, observation['task_id']): {
        'target': {'id': row.remote_id}, 'result': deepcopy(observation),
        'updated_at': STAMP.replace(tzinfo=None) + timedelta(microseconds=3)}}


def test_manual_sync_microsecond_order_uses_exact_target_result_and_completion_evidence():
    row = distribution(quota=1250000)
    proof = manual_proof(row)
    assert total(row)['covered'] == 0
    assert total(row, evidence=proof)['amount'] == '2.5'
    row.last_sync_at += timedelta(seconds=1)
    assert total(row, evidence=proof)['covered'] == 0  # Later readback/new generation.


@pytest.mark.parametrize('change', ['target', 'time', 'usage', 'completion', 'task', 'distribution'])
def test_manual_evidence_must_match_this_observation_and_target(change):
    row = distribution(quota=500000)
    evidence = manual_proof(row)
    key = next(iter(evidence))
    proof = evidence[key]
    if change == 'target':
        proof['target']['id'] = 'old-target'
    elif change == 'time':
        proof['result']['synced_at'] = STAMP.isoformat()
    elif change == 'usage':
        proof['result']['remote_usage'] = usage(1000000)
    elif change == 'completion':
        proof['updated_at'] = None
    elif change == 'task':
        evidence[(row.id, 'other-task')] = evidence.pop(key)
    else:
        evidence[('other-distribution', 'sync-task')] = evidence.pop(key)
    assert total(row, evidence=evidence)['amount'] is None


def test_mock_manual_sync_zero_then_updated_amount_reaches_api_without_changing_facts(db, login, observed):
    observed.usage_value = usage(0)
    monitor_action(db, observed, operation='sync_usage')
    assert worker.run_once(client=object())
    db.expire_all()
    dist = db.get(Distribution, observed.dist.id)
    observation = dist.remote_snapshot['_monitoring']['usage_sync']
    assert datetime.fromisoformat(observation['synced_at'].replace('Z', '+00:00')).replace(tzinfo=None) <= dist.last_sync_at
    path = '/api/channels/' + observed.channel.id
    first = observed.client.get(path).json()
    assert first['remote_usage_total'] == {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 3}
    assert first['verified_usage_by_unit'] == {} and first['fact_count'] == 0
    assert login('admin').get(path).json()['remote_usage_total'] == first['remote_usage_total']
    assert login('other_admin').get(path).status_code == 404
    observed.usage_value = usage(1250000)
    monitor_action(db, observed, operation='sync_usage')
    assert worker.run_once(client=object())
    final = observed.client.get(path).json()
    assert final['remote_usage_total']['amount'] == '2.5'
    assert final['verified_usage_by_unit'] == {} and final['fact_count'] == 0
    assert db.scalar(select(func.count()).select_from(UsageFact)) == 0
    assert len(observed.reads) == 2 and not observed.tests
    assert 'test-only-seller-token' not in str(final['remote_usage_total'])


def test_manual_evidence_query_is_bounded_for_twenty_channels_and_list_matches_detail(db, login, setup_catalog):
    client = login('user')
    submitted = client.post('/api/uploads/submit', json=payload(setup_catalog,
        keys='\n'.join(f'placeholder-total-key-{i}' for i in range(20))))
    assert submitted.status_code == 200, submitted.text
    task = db.get(Task, submitted.json()['id'])
    task.status = 'succeeded'
    for index, item in enumerate(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id))):
        dist = db.get(Distribution, item.distribution_id)
        row = distribution(str(index + 1), quota=500000)
        row.remote_snapshot['_monitoring']['usage_sync']['task_id'] = task.id
        proof = next(iter(manual_proof(row).values()))
        dist.status, dist.remote_id = row.status, row.remote_id
        dist.remote_snapshot, dist.last_sync_at = row.remote_snapshot, row.last_sync_at
        item.operation, item.status, item.updated_at = 'sync_usage', 'succeeded', proof['updated_at']
        item.snapshot = {'operation_target': proof['target'], 'operation_result': proof['result']}
    db.commit()

    def query_count(limit):
        statements = []

        def record(*args):
            statements.append(args[2])

        event.listen(engine, 'before_cursor_execute', record)
        try:
            response = client.get(f'/api/channels?limit={limit}')
            assert response.status_code == 200, response.text
            rows = response.json()['items']
            assert len(rows) == limit
            assert all(row['remote_usage_total'] == {
                'amount': '3', 'unit': 'USD', 'covered': 3, 'total': 3} for row in rows)
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        return len(statements), rows

    count_one, _ = query_count(1)
    count_twenty, rows = query_count(20)
    assert count_twenty <= count_one + 1
    detail = client.get('/api/channels/' + rows[0]['id']).json()
    assert detail['remote_usage_total'] == rows[0]['remote_usage_total']
