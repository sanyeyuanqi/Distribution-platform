"""Force deletion confirms the remote result before removing scoped local data."""
# ruff: noqa: F811
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, validate_delete_identity
from app.db import utcnow
from app.force_deletion import remote_delete_target
from app.models import Site
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    KeyVersion,
    TaskItem,
    UploadGroup,
)
from app.security import encrypt
from settlement_fixtures import order_for
from sqlalchemy import func, select
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_force_delete_local import fact_for
from test_newapi_adapter import RemoteFixture
from test_tcp_red import colin  # noqa: F401


def enqueue(case, ids=None, **extra):
    ids = ids or [case.dist.id]
    return case.client.post('/api/channels/' + case.channel.id + '/actions', json={
        'action': 'force_delete', 'distribution_ids': ids,
        'confirmation': f'DELETE REMOTE AND LOCAL {len(ids)}', **extra})


@pytest.fixture
def force_remote(db, delete_case, monkeypatch):
    state = SimpleNamespace(rows={d.remote_id: deepcopy(d.remote_snapshot) for d in db.scalars(
        select(Distribution).where(Distribution.channel_id == delete_case.channel.id))}, writes=[], fail=None)

    class Adapter:
        def __init__(self, before_write): self.before_write = before_write
        def permissions(self): return {'can_write': True}
        def _observation_owned(self, remote, permissions): pass
        def detail(self, remote_id):
            if state.fail == 'read':
                raise RemoteError('unavailable', category='permission_denied', status_code=403)
            if remote_id not in state.rows:
                raise RemoteError('ambiguous missing', status_code=404)
            return deepcopy(state.rows[remote_id])
        def find_unique_name(self, name):
            return next((deepcopy(row) for row in state.rows.values() if row['name'] == name), None)
        def delete(self, remote_id, *, expected):
            validate_delete_identity(self.detail(remote_id), expected)
            self.before_write()
            state.writes.append(remote_id)
            if state.fail == 'timeout':
                raise RemoteError('unknown delete result', unknown=True)
            state.rows.pop(remote_id)

    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write=None: Adapter(before_write))
    monkeypatch.setattr('app.routers.channels.notify_worker', lambda: None)
    return state


@pytest.mark.parametrize('role', ['other_user', 'other_admin', 'sibling'])
def test_force_cannot_cross_local_owner_scope(db, login, delete_case, role):
    case = delete_case
    case.client = login(role)
    assert enqueue(case).status_code == 404


@pytest.mark.parametrize('confirmation', [None, 'HARD DELETE 1', 'FORCE DELETE 1', 'DELETE 1'])
def test_force_requires_new_remote_and_local_confirmation(delete_case, confirmation):
    assert enqueue(delete_case, confirmation=confirmation).status_code == 422


def test_cached_local_only_action_never_changes_semantics(delete_case):
    assert enqueue(delete_case, action='force_delete_local', confirmation='HARD DELETE 1').status_code == 422


def test_partial_force_deletes_remote_then_local_and_keeps_other_partitions(db, delete_case, force_remote):
    case = delete_case
    dist_id, remote_id, channel_id = case.dist.id, case.dist.remote_id, case.channel.id
    response = enqueue(case, idempotency_key='force-exact-one')
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert db.get(Distribution, dist_id) is not None and not force_remote.writes
    reopened = case.client.get('/api/channels/' + channel_id).json()
    assert reopened['force_delete_task'] == {'id': task_id, 'status': 'queued'}
    assert worker.run_once()
    db.expire_all()
    assert force_remote.writes == [remote_id]
    assert db.get(Distribution, dist_id) is None and db.get(Channel, channel_id) is not None
    result = case.client.get('/api/channels/action-tasks/' + task_id).json()
    assert result['status'] == 'succeeded'
    assert result['result'] == {'deleted_distribution_count': 1, 'deleted_channel_count': 0, 'remote_confirmed': True}
    assert result['items'][0]['channel_id'] is None and result['items'][0]['distribution_id'] is None
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


@pytest.mark.parametrize('acknowledged', [False, True])
def test_build_expiry_blocks_remote_delete_but_not_acknowledged_local_cleanup(db, delete_case, force_remote, acknowledged):
    from app.adapters.newapi_builds import BUILD, BUILD_ID, ORIGIN

    case = delete_case
    site = db.get(Site, case.dist.site_id)
    site.adapter, site.base_url, site.verified_at = 'new-api-v1', ORIGIN, utcnow()
    site.capabilities = {**site.capabilities, 'verified_version': BUILD_ID, 'verified_build': dict(BUILD)}
    db.commit()
    response = enqueue(case)
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    dist_id = case.dist.id
    assert item.snapshot['site_build_id'] == BUILD_ID
    if acknowledged:
        item.snapshot = {**item.snapshot, 'delete_acknowledged': True}
        item.stage = 'remote_deleted_pending_purge'
        case.dist.status = 'deleted'
        force_remote.rows.pop(case.dist.remote_id)
    site.capabilities = {'verification_error': {'category': 'connection_error'}}
    site.verified_at = None
    db.commit()
    if acknowledged:
        worker.execute_item(db, item)
        db.expire_all()
        assert db.get(Distribution, dist_id) is None
    else:
        with pytest.raises(worker.WriteStopped, match='构建'):
            worker.execute_item(db, item)
        assert db.get(Distribution, dist_id) is not None
    assert not force_remote.writes


def test_final_force_removes_key_fingerprints_and_only_retains_count_result(db, delete_case, force_remote):
    case = delete_case
    channel_id = case.channel.id
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == channel_id)))
    request = enqueue(case, ids, idempotency_key='force-final-all')
    assert request.status_code == 200, request.text
    for _ in ids:
        assert worker.run_once()
    db.expire_all()
    for model in (Channel, ChannelCredential, KeyVersion, Distribution, UploadGroup):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    items = list(db.scalars(select(TaskItem)))
    assert len(items) == len(ids)
    assert all(set(item.snapshot) == {'operation_result'} for item in items)
    assert all(item.channel_id is item.distribution_id is item.key_version is item.proxy_encrypted is None for item in items)
    assert len(force_remote.writes) == len(ids)
    # A lost HTTP response can be recovered even after the source channel is gone.
    again = case.client.post('/api/channels/' + channel_id + '/actions', json={
        'action': 'force_delete', 'distribution_ids': ids, 'confirmation': f'DELETE REMOTE AND LOCAL {len(ids)}',
        'idempotency_key': 'force-final-all'})
    assert again.status_code == 200 and again.json()['id'] == request.json()['id']
    assert again.json()['result']['deleted_channel_count'] == 1


@pytest.mark.parametrize('fail', ['read', 'timeout'])
def test_unknown_or_permission_failure_keeps_local_and_never_replays_delete(db, delete_case, force_remote, fail):
    case = delete_case
    dist_id = case.dist.id
    force_remote.fail = fail
    result = enqueue(case).json()
    assert worker.run_once()
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.status == 'needs_review'
    prior_writes = len(force_remote.writes)
    force_remote.fail = None
    assert case.client.post('/api/tasks/' + result['id'] + '/reconcile').status_code == 200
    assert worker.run_once()
    assert len(force_remote.writes) == prior_writes


def test_acknowledged_delete_survives_cleanup_block_and_retry_without_second_delete(db, users, delete_case, force_remote, monkeypatch):
    from app.force_deletion import purge_distributions as real_purge
    from fastapi import HTTPException

    case = delete_case
    result = enqueue(case).json()
    def blocked(*args, **kwargs):
        if not kwargs.get('validate_only'):
            raise HTTPException(409, '关联账单正在处理')
        return real_purge(*args, **kwargs)
    monkeypatch.setattr('app.force_deletion.purge_distributions', blocked)
    assert worker.run_once()
    db.expire_all()
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.snapshot['delete_acknowledged'] is True and item.status == 'failed'
    assert db.get(Distribution, case.dist.id) is not None
    monkeypatch.setattr('app.force_deletion.purge_distributions', real_purge)
    assert case.client.post('/api/tasks/' + result['id'] + '/retry').status_code == 200
    assert worker.run_once()
    assert len(force_remote.writes) == 1


def test_settlement_order_snapshot_rejects_before_any_remote_delete(db, users, delete_case, force_remote):
    distribution_ids = list(db.scalars(select(Distribution.id).where(
        Distribution.channel_id == delete_case.channel.id)))
    fact = fact_for(db, users, delete_case.channel, delete_case.dist)
    order, line = order_for(db, users, delete_case.channel, sources=[{
        'distribution_id': delete_case.dist.id, 'site_id': delete_case.dist.site_id,
        'usage_amount': '8', 'payment_amount': '8',
    }])
    snapshot = deepcopy(line.site_amounts)
    snapshot_hash = order.snapshot_hash
    db.commit()
    result = enqueue(delete_case, ids=distribution_ids)
    assert result.status_code == 409 and not force_remote.writes
    assert all(db.get(Distribution, distribution_id) is not None for distribution_id in distribution_ids)
    db.refresh(order)
    db.refresh(line)
    db.refresh(fact)
    assert order.snapshot_hash == snapshot_hash and line.site_amounts == snapshot
    assert fact.distribution_id == delete_case.dist.id


def test_deleted_marker_and_archived_channel_still_require_remote_confirmation(db, delete_case, force_remote):
    delete_case.channel.archived = True
    delete_case.dist.status = 'deleted'
    delete_case.dist.remote_snapshot = {**delete_case.dist.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    db.get(Site, delete_case.dist.site_id).enabled = False
    dist_id = delete_case.dist.id
    db.commit()
    assert enqueue(delete_case).status_code == 200
    assert worker.run_once()
    db.expire_all()
    assert len(force_remote.writes) == 1 and db.get(Distribution, dist_id) is None


def test_owner_admin_can_poll_only_scoped_delete_task_without_task_center_access(db, login, delete_case, force_remote):
    result = enqueue(delete_case).json()
    path = '/api/channels/action-tasks/' + result['id']
    assert login('admin').get(path).status_code == 200
    assert login('admin').get('/api/tasks/' + result['id']).status_code == 403
    assert login('other_admin').get(path).status_code == 404


@pytest.mark.parametrize('reappeared', [False, True])
def test_prior_ordinary_delete_ack_only_applies_while_local_state_still_deleted(db, delete_case, force_remote, reappeared):
    case = delete_case
    response = case.client.post('/api/channels/' + case.channel.id + '/actions', json={
        'action': 'delete_remote', 'distribution_ids': [case.dist.id], 'confirmation': 'DELETE 1'})
    assert response.status_code == 200
    assert worker.run_once()
    db.expire_all()
    dist_id = case.dist.id
    assert db.get(Distribution, dist_id).status == 'deleted'
    if reappeared:
        force_remote.rows[case.dist.remote_id] = deepcopy(case.dist.remote_snapshot)
        case.dist.status = 'enabled'
        db.commit()
    else:
        force_remote.fail = 'read'  # The persisted exact DELETE acknowledgement is sufficient.
    assert enqueue(case).status_code == 200
    assert worker.run_once()
    db.expire_all()
    assert db.get(Distribution, dist_id) is None and len(force_remote.writes) == (2 if reappeared else 1)


@pytest.mark.parametrize(('status', 'message', 'missing'), [
    (200, 'record not found', True), (404, 'record not found', False),
    (200, 'permission denied', False), (403, 'record not found', False),
])
def test_only_explicit_reviewed_official_not_found_can_confirm_absence(status, message, missing):
    server = RemoteFixture('v1.0.0-rc.35')
    def transport(request):
        if request.url.path == '/api/channel/91':
            return httpx.Response(status, json={'success': False, 'message': message})
        return server.handle(request)
    site = SimpleNamespace(base_url='https://fixture.invalid', seller_user_id='71', token_encrypted=encrypt('fixture-token'))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = NewAPIAdapter(site, transport=client.request)
        target = {'id': '91', 'name': 'frozen-name', 'type': 1}
        if missing:
            assert remote_delete_target(adapter, target) is None
        else:
            with pytest.raises(RemoteError) as caught:
                remote_delete_target(adapter, target)
            assert caught.value.unknown
    assert not server.writes


def test_completed_purge_after_worker_crash_only_publishes_terminal_status(db, delete_case, force_remote):
    result = enqueue(delete_case).json()
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status, item.lease_until = 'running', utcnow()
    db.commit()
    worker.execute_item(db, item)
    db.refresh(item)
    assert item.channel_id is None and item.snapshot['operation_result']['remote_confirmed'] is True
    worker.execute_item(db, item)
    assert len(force_remote.writes) == 1


@pytest.mark.parametrize('foreign_owner', [False, True])
def test_colin_force_probe_preserves_ownership_and_exact_delete_route(colin, foreign_owner):
    colin.server.remote['base_url'] = '***'
    target = {'id': '77', 'name': 'stable-fixture', 'type': 1}
    guard_calls = []
    colin.adapter.before_write = lambda: guard_calls.append('before_write')
    if foreign_owner:
        colin.server.remote['created_by'] = 999
        with pytest.raises(RemoteError):
            remote_delete_target(colin.adapter, target)
        assert not colin.server.writes and not guard_calls
        return
    assert remote_delete_target(colin.adapter, target)['id'] == 77
    colin.adapter.delete('77', expected=target)
    request, = colin.server.writes
    assert request.method == 'DELETE' and request.url.path == '/api/channel/77'
    assert guard_calls == ['before_write']
    assert '***' not in request.content.decode()


@pytest.mark.parametrize('history', ['never_sent', 'uncertain_found', 'uncertain_missing'])
def test_missing_remote_id_needs_create_history_and_unique_name_evidence(db, delete_case, force_remote, history):
    case = delete_case
    dist_id, remote_id = case.dist.id, case.dist.remote_id
    previous = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist_id))
    previous.remote_write_attempted = history != 'never_sent'
    case.dist.remote_id = None
    if history in ('never_sent', 'uncertain_missing'):
        force_remote.rows.pop(remote_id)
    db.commit()
    task = enqueue(case).json()
    assert worker.run_once()
    db.expire_all()
    if history == 'uncertain_missing':
        assert db.get(Distribution, dist_id) is not None
        assert db.get(TaskItem, task['items'][0]['id']).status == 'needs_review'
        assert not force_remote.writes
    else:
        assert db.get(Distribution, dist_id) is None
        assert force_remote.writes == ([remote_id] if history == 'uncertain_found' else [])


@pytest.mark.parametrize(('operation', 'snapshot', 'never_created'), [
    ('test', {'test_source': 'local'}, True), ('test', {}, False),
    ('test', {'test_source': 'remote'}, False), ('create', {'test_source': 'local'}, False),
])
def test_only_local_model_probe_is_excluded_from_remote_creation_history(db, delete_case, force_remote,
                                                                         operation, snapshot, never_created):
    case = delete_case
    dist_id, remote_id = case.dist.id, case.dist.remote_id
    original = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist_id))
    original.status, original.stage, original.remote_write_attempted = 'failed', 'queued', False
    case.dist.remote_id = None
    force_remote.rows.pop(remote_id)
    db.add(TaskItem(task_id=original.task_id, channel_id=case.channel.id, distribution_id=dist_id,
        site_id=case.dist.site_id, operation=operation, snapshot=snapshot,
        status='succeeded', stage='complete', remote_write_attempted=True))
    db.commit()
    response = enqueue(case)
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    assert item.snapshot['remote_never_created'] is never_created
    assert worker.run_once()
    db.expire_all()
    assert (db.get(Distribution, dist_id) is None) is never_created
    assert not force_remote.writes
