"""A row toggle selects one distribution, including same-site credential partitions."""
# ruff: noqa: F811
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.models_channels import Channel, Distribution, Task, TaskItem
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401
from test_multikey_containers import (
    AWS_KEYS,
    complete,
    container_site,  # noqa: F401
    post,
    remotes,  # noqa: F401
    request_body,
    setup_template,
)


@pytest.fixture
def toggle_case(db, login, container_site, remotes, monkeypatch):
    site = container_site
    site.adapter = 'silicon-v1'
    db.commit()
    root, member = login('root'), login('user')
    category, fmt = setup_template(db, root, site)
    result = post(member, request_body(category, fmt, AWS_KEYS))
    complete(db, result)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id)
                                    .order_by(Distribution.id)))
    assert len(distributions) == 2 and len({dist.partition_key for dist in distributions}) == 2
    records = {remote_id: record for (site_id, remote_id), record in remotes[0].items() if site_id == site.id}
    for dist in distributions:
        records[dist.remote_id]['status'] = 1
        dist.status = 'enabled'
        dist.remote_snapshot = {**dist.remote_snapshot, 'status': 1}
    db.commit()
    state = SimpleNamespace(can_toggle=True, apply_status=True, calls=[], writes=[])
    def handle(request):
        path = request.url.path
        state.calls.append((request.method, path))
        if request.method == 'POST':
            assert path.startswith('/api/seller/channel/') and path.endswith('/status')
            remote_id = path.split('/')[-2]
            body = json.loads(request.content)
            state.writes.append((remote_id, body))
            if state.apply_status:
                records[remote_id]['status'] = getattr(state, 'readback_status', body['status'])
            return httpx.Response(200, json={'success': True})
        assert request.method == 'GET'
        if path == '/api/user/self':
            data = {'id': int(site.seller_user_id)}
        elif path == '/api/status':
            data = {'version': 'v99.1.0-fixture'}
        elif path == '/api/seller/channel/':
            data = {'items': list(records.values()), 'total': len(records),
                    'can_write': True, 'can_toggle': state.can_toggle, 'can_edit_routing': True}
        elif path == '/api/seller/channel/meta':
            data = {'models': [{'id': 'fixture-model'}], 'groups': ['default']}
        else:
            assert path.startswith('/api/seller/channel/')
            if getattr(state, 'detail_unavailable', False):
                return httpx.Response(503, json={'success': False})
            data = records[path.rsplit('/', 1)[-1]]
        return httpx.Response(200, json={'success': True, 'data': data})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(worker, 'get_adapter', lambda target_site, before_write:
                            SiliconAdapter(target_site, transport=client.request, before_write=before_write))
        yield SimpleNamespace(site=site, channel=channel, selected=distributions[0], sibling=distributions[1],
                              member=member, state=state, records=records, category=category, fmt=fmt)


def action(client, case, action_name='disable', **values):
    return client.post(f'/api/channels/{case.channel.id}/actions', json={
        'action': action_name, 'distribution_ids': [case.selected.id], **values})


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_disable_then_enable_only_selected_same_site_partition(db, login, toggle_case, role):
    case, client = toggle_case, login(role)
    channel_before = {field: deepcopy(getattr(case.channel, field)) for field in
                      ('archived', 'models', 'key_count', 'key_version', 'key_encrypted', 'upload_settings')}
    sibling_before = {field: deepcopy(getattr(case.sibling, field)) for field in
                      ('status', 'remote_id', 'remote_snapshot', 'models', 'last_sync_at', 'key_version')}
    for action_name, desired, expected_status in [('disable', 2, 'disabled'), ('enable', 1, 'enabled')]:
        response = action(client, case, action_name)
        assert response.status_code == 200, response.text
        task = response.json()
        assert task['kind'] == action_name and len(task['items']) == 1
        item = db.get(TaskItem, task['items'][0]['id'])
        assert item.distribution_id == case.selected.id and item.site_id == case.site.id
        assert item.snapshot['partition_key'] == case.selected.partition_key
        assert case.selected.status != expected_status  # Enqueueing is not an acknowledgement.
        complete(db, task)
        db.refresh(case.selected)
        db.refresh(case.sibling)
        assert case.selected.status == expected_status
        assert case.selected.remote_snapshot['status'] == desired
        assert case.state.writes[-1] == (case.selected.remote_id, {'status': desired})
        assert {field: getattr(case.sibling, field) for field in sibling_before} == sibling_before
    assert len(case.state.writes) == 2
    db.refresh(case.channel)
    assert {field: getattr(case.channel, field) for field in channel_before} == channel_before
    assert case.site.enabled is True


@pytest.mark.parametrize('role', ['other_admin', 'other_user', 'sibling'])
def test_other_accounts_cannot_toggle_owner_distribution(db, login, toggle_case, role):
    before = db.scalar(select(func.count()).select_from(Task))
    assert action(login(role), toggle_case).status_code == 404
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert not toggle_case.state.calls and toggle_case.selected.status == 'enabled'


def test_toggle_rejects_cross_channel_id_and_mixed_target_selectors(db, toggle_case):
    case = toggle_case
    other = post(case.member, request_body(case.category, case.fmt, 'another-fixture-key',
                                          idempotency_key='different-channel-fixture'))
    foreign_id = other['items'][0]['distribution_id']
    before = db.scalar(select(func.count()).select_from(Task))
    assert action(case.member, case, distribution_ids=[foreign_id]).status_code == 422
    assert action(case.member, case, site_ids=[case.site.id]).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert not case.state.calls


def test_remote_toggle_permission_is_checked_before_any_post(db, toggle_case):
    case = toggle_case
    response = action(case.member, case)
    assert response.status_code == 200
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    case.state.can_toggle = False
    with pytest.raises(RemoteError, match='启停权限'):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted and not case.state.writes
    assert case.selected.status == case.sibling.status == 'enabled'


def test_mismatched_readback_does_not_claim_selected_channel_disabled(db, toggle_case):
    case = toggle_case
    response = action(case.member, case)
    assert response.status_code == 200
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    case.state.apply_status = False
    with pytest.raises(RemoteError, match='回读状态与目标不一致'):
        worker.execute_item(db, item)
    assert item.remote_write_attempted and item.stage == 'updated_pending_verification'
    assert case.state.writes == [(case.selected.remote_id, {'status': 2})]
    assert case.selected.status == case.sibling.status == 'enabled'


@pytest.fixture
def worker_lease(monkeypatch):
    class Lease:
        def __init__(self, *args): pass
        def acquire(self): return True
        def check(self): pass
        def close(self): pass

    monkeypatch.setattr(worker, 'SiteLease', Lease)


@pytest.mark.parametrize('reconcile', [False, True], ids=['fresh-write', 'readonly-reconcile'])
@pytest.mark.parametrize(('operation', 'raw_status', 'outcome', 'remote_status'), [
    ('disable', 2, 'succeeded', 'disabled'),
    ('disable', 3, 'succeeded', 'disabled'),
    ('enable', 1, 'succeeded', 'enabled'),
    ('disable', 1, 'failed', 'enabled'),
    ('enable', 2, 'failed', 'disabled'),
    ('enable', 3, 'failed', 'disabled'),
    ('disable', 0, 'needs_review', 'unavailable'),
    ('enable', 7, 'needs_review', 'unavailable'),
    ('disable', '3', 'needs_review', None),
    ('enable', True, 'needs_review', None),
])
def test_worker_toggle_status_semantics_and_terminal_ledger(
        db, toggle_case, worker_lease, operation, raw_status, outcome, remote_status, reconcile):
    case = toggle_case
    response = action(case.member, case, operation)
    assert response.status_code == 200, response.text
    item_id = response.json()['items'][0]['id']
    task_id = response.json()['id']
    if reconcile:
        item = db.get(TaskItem, item_id)
        item.status, item.stage, item.remote_write_attempted = 'needs_review', 'updated_pending_verification', True
        item.error = '回读状态与目标不一致；本项已转为失败，可明确重试'
        db.get(Task, task_id).status = 'needs_review'
        case.records[case.selected.remote_id]['status'] = raw_status
        db.commit()
        result = case.member.post(f'/api/tasks/{task_id}/reconcile')
        assert result.status_code == 200, result.text
    else:
        case.state.readback_status = raw_status
    assert worker.run_once(client=object())
    db.expire_all()
    item, task = db.get(TaskItem, item_id), db.get(Task, task_id)
    assert item.status == task.status == outcome
    assert item.remote_write_attempted is True
    if remote_status is not None:
        selected = db.get(Distribution, case.selected.id)
        assert selected.status == remote_status and selected.remote_snapshot['status'] == raw_status
    if outcome == 'failed':
        assert '可明确重试' in item.error
    elif outcome == 'succeeded':
        assert item.error is None
    assert len(case.state.writes) == (0 if reconcile else 1)
    assert db.get(Distribution, case.sibling.id).status == 'enabled'
    assert not worker.run_once(client=object())  # No automatic replay on mismatch or unknown.


@pytest.mark.parametrize('reconcile', [False, True])
def test_toggle_readback_unavailable_keeps_unknown_write_without_resending(db, toggle_case, worker_lease, reconcile):
    case = toggle_case
    response = action(case.member, case)
    assert response.status_code == 200
    item_id, task_id = response.json()['items'][0]['id'], response.json()['id']
    if reconcile:
        item = db.get(TaskItem, item_id)
        item.status, item.stage, item.remote_write_attempted = 'needs_review', 'updated_pending_verification', True
        db.get(Task, task_id).status = 'needs_review'
        db.commit()
        assert case.member.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
    case.state.detail_unavailable = True
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item_id).status == 'needs_review'
    assert db.get(Task, task_id).status == 'needs_review'
    assert len(case.state.writes) == (0 if reconcile else 1)
    assert not worker.run_once(client=object())


def test_confirmed_mismatch_unblocks_channel_and_can_be_explicitly_retried(db, toggle_case, worker_lease):
    case = toggle_case
    result = action(case.member, case).json()
    case.state.readback_status = 1
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, result['items'][0]['id']).status == 'failed'
    response = case.member.post(f'/api/tasks/{result["id"]}/retry')
    assert response.status_code == 200, response.text
    case.state.readback_status = 3
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(Task, result['id']).status == 'succeeded'
    assert len(case.state.writes) == 2
    # An automatic-disable readback also leaves the channel ready for another action.
    assert action(case.member, case, 'enable').status_code == 200
