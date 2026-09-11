"""Archiving requires fresh, read-only confirmation of every linked remote."""
# ruff: noqa: F811
from types import SimpleNamespace

import httpx
import pytest
from app.adapters.silicon import SiliconAdapter
from app.channel_archive import archive_projection
from app.db import uid, utcnow
from app.models import AuditEvent
from app.models_billing import SettlementOrder, SettlementOrderLine, UsageFact
from app.models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    Task,
    TaskItem,
)
from settlement_fixtures import order_for
from sqlalchemy import func, select
from test_channels import (  # noqa: F401 - shared isolated fixture
    payload,
    response,
    setup_catalog,
)


def observation(**changes):
    values = {'remote_id': '701', 'remote_name': 'known-channel', 'status': 'disabled',
              'remote_snapshot': {'id': 701, 'name': 'known-channel', 'type': 1, 'status': 2}}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('raw_status', [2, 3])
def test_projection_accepts_only_confirmed_disabled_observations(raw_status):
    dist = observation()
    dist.remote_snapshot['status'] = raw_status
    assert archive_projection(SimpleNamespace(archived=False), [dist]) == {
        'archive_available': True, 'archive_reason': ''}


@pytest.mark.parametrize('case', [
    'no_distributions', 'already_archived', 'no_remote', 'missing', 'deleted', 'tombstone',
    'identity_id', 'identity_name', 'identity_type', 'enabled', 'unknown', 'string_status', 'bool_status',
])
def test_projection_explains_why_unconfirmed_channels_cannot_be_archived(case):
    channel = SimpleNamespace(archived=case == 'already_archived')
    dist = observation()
    if case == 'no_remote':
        dist.remote_id = None
    elif case in ('missing', 'deleted'):
        dist.status = case
    elif case == 'tombstone':
        dist.remote_snapshot['_local_deletion'] = {'remote_confirmed': False}
    elif case.startswith('identity_'):
        field = case.removeprefix('identity_')
        dist.remote_snapshot[field] = {'id': 999, 'name': 'another-channel', 'type': '1'}[field]
    elif case in ('enabled', 'unknown', 'string_status', 'bool_status'):
        dist.remote_snapshot['status'] = {'enabled': 1, 'unknown': None,
                                          'string_status': '2', 'bool_status': True}[case]
    result = archive_projection(channel, [] if case == 'no_distributions' else [dist])
    assert result['archive_available'] is False
    assert result['archive_reason']


@pytest.fixture
def archive_case(db, login, setup_catalog, monkeypatch):
    client = login('user')
    uploaded = client.post('/api/uploads/submit', json=payload(setup_catalog))
    assert uploaded.status_code == 200, uploaded.text
    uploaded = uploaded.json()
    channel = db.get(Channel, uploaded['items'][0]['channel_id'])
    items = list(db.scalars(select(TaskItem).where(TaskItem.channel_id == channel.id)))
    for item in items:
        item.status = 'succeeded'
    db.get(Task, uploaded['id']).status = 'succeeded'
    distributions = list(db.scalars(select(Distribution).where(
        Distribution.channel_id == channel.id).order_by(Distribution.id)))
    state = SimpleNamespace(client=client, channel=channel, distributions=distributions,
                            items=items, remotes={}, calls=[], detail_reads=[], error=None)
    for index, dist in enumerate(distributions):
        dist.remote_id, dist.status = str(701 + index), 'disabled'
        dist.remote_snapshot = {'id': 701 + index, 'name': dist.remote_name, 'type': 1,
                                'status': 2, 'models': 'test-model', 'group': 'default'}
        state.remotes[(dist.site_id, dist.remote_id)] = dict(dist.remote_snapshot)
    db.commit()

    def adapter(site):
        def transport(method, url, **kwargs):
            state.calls.append((method, url))
            assert method == 'GET', 'Archiving must never change the remote channel'
            if url.endswith('/api/user/self'):
                return response({'id': 123})
            if url.endswith('/api/status'):
                return response({'version': SiliconAdapter.VERSION})
            if url.endswith('/meta'):
                return response({'models': [{'id': 'test-model'}], 'groups': ['default']})
            if url.endswith('/api/seller/channel/'):
                return response({'can_write': True, 'can_toggle': True,
                                 'can_edit_routing': False, 'items': [], 'total': 0})
            remote_id = url.rsplit('/', 1)[-1]
            key = (site.id, remote_id)
            assert key in state.remotes, 'Unexpected mock endpoint'
            state.detail_reads.append(key)
            if state.error == 'timeout':
                raise httpx.ReadTimeout('Test transport timeout; no live request')
            if state.error == 'not_found':
                return response(None, status=404)
            return response(state.remotes[key])
        return SiliconAdapter(site, transport=transport)

    monkeypatch.setattr('app.channel_archive.get_adapter', adapter)
    state.path = '/api/channels/' + channel.id + '/actions'
    return state


def archive_audits(db, channel_id):
    return list(db.scalars(select(AuditEvent).where(
        AuditEvent.action == 'channel.archive', AuditEvent.object_id == channel_id)))


def assert_unchanged_after_rejection(db, case, result, expected_status=409):
    assert result.status_code == expected_status, result.text
    db.expire_all()
    assert db.get(Channel, case.channel.id).archived is False
    assert archive_audits(db, case.channel.id) == []


def test_archive_checks_all_sites_and_preserves_usage_orders_and_history(db, users, archive_case):
    case = archive_case
    dist = case.distributions[0]
    case.remotes[(case.distributions[-1].site_id, case.distributions[-1].remote_id)]['status'] = 3
    now = utcnow()
    history = DistributionVersion(distribution_id=dist.id, key_version=1)
    fact = UsageFact(id=uid(), source_id='archive-history-fixture', site_id=dist.site_id,
        distribution_id=dist.id, channel_id=case.channel.id, owner_id=users['user'].id,
        admin_id=users['admin'].id, category_id=case.channel.category_id, occurred_at=now,
        raw_amount=8, raw_unit='USD', amount=8, unit='USD', conversion_version='fixture', verified=True)
    db.add_all([history, fact])
    db.flush()
    order, line = order_for(db, users, case.channel, sources=[{'fact_id': fact.id}])
    db.commit()
    task_count = db.scalar(select(func.count()).select_from(Task))

    result = case.client.post(case.path, json={'action': 'archive'})
    assert result.status_code == 200, result.text
    assert result.json()['archived'] is True
    assert result.json()['archive_available'] is False
    assert set(case.detail_reads) == set(case.remotes)
    assert len(case.detail_reads) == 3
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Task)) == task_count
    for model, row in [(DistributionVersion, history), (UsageFact, fact), (SettlementOrder, order),
                       (SettlementOrderLine, line)]:
        assert db.get(model, row.id) is not None
    assert db.get(DistributionVersion, history.id).valid_to is None
    assert db.get(UsageFact, fact.id).amount == 8
    assert db.get(SettlementOrderLine, line.id).site_amounts == [{'fact_id': fact.id}]
    assert db.get(SettlementOrder, order.id).status == 'settled'
    assert case.client.get('/api/channels/' + case.channel.id).json()['usage_by_unit'] == {'USD': '8.00000000'}
    audit, = archive_audits(db, case.channel.id)
    checks = audit.summary['remote_status_checks']
    assert {check['distribution_id'] for check in checks} == {dist.id for dist in case.distributions}
    assert {check['status'] for check in checks} == {2, 3}
    assert all(check['checked_at'] for check in checks)

    # Restoring visibility must also work offline and never enable a remote or
    # change an existing order. Replaying the request must not duplicate audit.
    snapshots = {row.id: dict(row.remote_snapshot) for row in case.distributions}
    case.calls.clear()
    case.error = 'timeout'
    for _ in range(2):
        restored = case.client.post(case.path, json={'action': 'unarchive'})
        assert restored.status_code == 200, restored.text
        assert restored.json()['archived'] is False
    assert case.calls == []
    db.expire_all()
    assert all(db.get(Distribution, row_id).remote_snapshot == snapshot for row_id, snapshot in snapshots.items())
    assert all(db.get(Distribution, row_id).status == 'disabled' for row_id in snapshots)
    assert db.get(UsageFact, fact.id).amount == 8
    assert db.get(SettlementOrderLine, line.id).site_amounts == [{'fact_id': fact.id}]
    assert db.get(SettlementOrderLine, line.id).order_id == order.id
    assert db.scalar(select(func.count()).select_from(Task)) == task_count
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == 'channel.unarchive', AuditEvent.object_id == case.channel.id)) == 1
    assert case.channel.id in {row['id'] for row in case.client.get('/api/channels').json()['items']}
    assert case.channel.id not in {row['id'] for row in case.client.get('/api/channels?archived=true').json()['items']}


@pytest.mark.parametrize('raw_status', [1, None, 0, '2', True])
def test_old_disabled_snapshot_cannot_override_current_remote_state(db, archive_case, raw_status):
    case = archive_case
    dist = case.distributions[-1]
    case.remotes[(dist.site_id, dist.remote_id)]['status'] = raw_status
    assert case.client.get('/api/channels/' + case.channel.id).json()['archive_available'] is True
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert set(case.detail_reads) == set(case.remotes)
    assert all(db.get(Distribution, row.id).remote_snapshot['status'] == 2 for row in case.distributions)


@pytest.mark.parametrize('changed', [{'id': 999}, {'name': 'reused-channel-id'}, {'type': 3}])
def test_archive_verifies_fresh_remote_identity(db, archive_case, changed):
    case = archive_case
    dist = case.distributions[0]
    case.remotes[(dist.site_id, dist.remote_id)].update(changed)
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert '归档' in result.json()['detail']


@pytest.mark.parametrize('error', ['timeout', 'not_found'])
def test_remote_read_failure_does_not_mean_disabled(db, archive_case, error):
    case = archive_case
    case.error = error
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert case.detail_reads


@pytest.mark.parametrize('local_state', ['no_remote', 'deleted', 'tombstone', 'identity_missing'])
def test_incomplete_or_deleted_association_is_rejected_before_remote_read(db, archive_case, local_state):
    case = archive_case
    dist = case.distributions[-1]
    if local_state == 'no_remote':
        dist.remote_id = None
    elif local_state == 'deleted':
        dist.status = 'deleted'
    elif local_state == 'tombstone':
        dist.remote_snapshot = {**dist.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    else:
        dist.remote_snapshot = {'id': int(dist.remote_id), 'status': 2}
    db.commit()
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert case.calls == []


@pytest.mark.parametrize('status', ['pending', 'running', 'needs_review'])
def test_unfinished_operation_blocks_archive_even_when_all_remotes_are_disabled(db, archive_case, status):
    case = archive_case
    case.items[0].status = status
    db.commit()
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert '任务' in result.json()['detail']
    assert case.calls == []


def test_archive_scope_cannot_exclude_another_site_or_same_site_partition(db, archive_case):
    case = archive_case
    first = case.distributions[0]
    extra = Distribution(id=uid(), channel_id=case.channel.id, site_id=first.site_id,
        partition_key='second', partition_label='Second partition', remote_id='999', remote_name='partition-999',
        status='disabled', models=['test-model'], remote_snapshot={
            'id': 999, 'name': 'partition-999', 'type': 1, 'status': 2})
    db.add(extra)
    db.commit()
    case.remotes[(extra.site_id, extra.remote_id)] = {**extra.remote_snapshot, 'status': 1}
    for scope in ({'site_ids': [first.site_id]}, {'distribution_ids': [first.id]}, {'site_ids': []}):
        result = case.client.post(case.path, json={'action': 'archive', **scope})
        assert_unchanged_after_rejection(db, case, result, 422)
    assert case.calls == []
    result = case.client.post(case.path, json={'action': 'archive'})
    assert_unchanged_after_rejection(db, case, result)
    assert (extra.site_id, extra.remote_id) in case.detail_reads
    case.remotes[(extra.site_id, extra.remote_id)]['status'] = 3
    case.detail_reads.clear()
    result = case.client.post(case.path, json={'action': 'archive'})
    assert result.status_code == 200, result.text
    assert set(case.detail_reads) == set(case.remotes)
    assert len(archive_audits(db, case.channel.id)) == 1


def test_archive_uses_existing_owner_permissions_before_remote_read(db, login, archive_case):
    case = archive_case
    for who in ('other_user', 'other_admin', 'sibling'):
        result = login(who).post(case.path, json={'action': 'archive'})
        assert_unchanged_after_rejection(db, case, result, 404)
    assert case.calls == []
    result = login('admin').post(case.path, json={'action': 'archive'})
    assert result.status_code == 200, result.text
    assert result.json()['archived'] is True


@pytest.mark.parametrize(('role', 'expected'), [('user', 200), ('admin', 200), ('root', 200), ('other_admin', 404)])
def test_unarchive_preserves_channel_management_permissions(db, login, archive_case, role, expected):
    case = archive_case
    case.channel.archived = True
    db.commit()
    result = login(role).post(case.path, json={'action': 'unarchive'})
    assert result.status_code == expected, result.text
    db.refresh(case.channel)
    assert case.channel.archived is (expected != 200)
    assert case.calls == []
