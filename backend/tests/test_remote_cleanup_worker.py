"""Async cleanup uses fake remotes and survives removal of its local source."""
# ruff: noqa: F811
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app import worker
from app.db import SessionLocal
from app.models import Site
from app.models_channels import Channel, Distribution, TaskItem, UnclaimedChannel
from sqlalchemy import select
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_force_delete_remote import force_remote  # noqa: F401


def enqueue(case, **overrides):
    response = case.client.post('/api/channels/' + case.channel.id + '/actions', json={
        'action': 'force_delete_async', 'distribution_ids': [case.dist.id],
        'confirmation': 'DELETE LOCAL AND QUEUE REMOTE 1', 'idempotency_key': 'cleanup-worker-001', **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def status(case, task):
    return case.client.get('/api/channels/action-tasks/' + task['id']).json()


def retry(case, task):
    return case.client.post('/api/channels/action-tasks/' + task['id'] + '/retry')


def test_worker_deletes_once_after_local_source_is_gone(db, delete_case, force_remote):
    case = delete_case
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == case.channel.id)))
    channel_id = case.channel.id
    task = enqueue(case, distribution_ids=ids, confirmation=f'DELETE LOCAL AND QUEUE REMOTE {len(ids)}')
    db.expire_all()
    assert db.get(Channel, channel_id) is None and not force_remote.writes
    for _ in ids:
        assert worker.run_once()
    result = status(case, task)
    assert result['status'] == 'succeeded' and result['result']['remote_confirmed'] is True
    assert len(force_remote.writes) == len(ids)
    assert retry(case, task).status_code == 409
    item = db.get(TaskItem, task['items'][0]['id'])
    db.refresh(item)
    item.status, item.stage = 'pending', 'write_sent'
    db.commit()
    assert worker.run_once()  # Lost final queue commit does not repeat acknowledged DELETE.
    assert len(force_remote.writes) == len(ids)


def test_timeout_auto_recovery_reads_only_explicit_retry_rechecks_then_deletes(db, delete_case, force_remote):
    case = delete_case
    task = enqueue(case)
    force_remote.fail = 'timeout'
    assert worker.run_once()
    assert status(case, task)['status'] == 'needs_review'
    assert len(force_remote.writes) == 1
    force_remote.fail = None
    item = db.get(TaskItem, task['items'][0]['id'])
    db.refresh(item)
    item.status = 'pending'
    db.commit()
    assert worker.run_once()
    assert len(force_remote.writes) == 1
    assert '仍存在' in status(case, task)['items'][0]['error']
    for endpoint in ('retry', 'reconcile'):
        assert case.client.post('/api/tasks/' + task['id'] + '/' + endpoint).status_code == 409
    assert retry(case, task).status_code == 200
    assert worker.run_once()
    assert len(force_remote.writes) == 2 and status(case, task)['status'] == 'succeeded'


def test_exact_absence_after_unknown_delete_finishes_without_resending(db, delete_case, force_remote, monkeypatch):
    task = enqueue(delete_case)
    force_remote.fail = 'timeout'
    assert worker.run_once()
    monkeypatch.setattr('app.force_deletion.remote_delete_target', lambda *_: None)
    assert retry(delete_case, task).status_code == 200
    assert worker.run_once()
    assert len(force_remote.writes) == 1
    assert status(delete_case, task)['result']['remote_confirmed'] is True


@pytest.mark.parametrize('found', [False, True])
def test_unknown_create_without_remote_id_never_deletes_same_name(db, delete_case, force_remote, found):
    case = delete_case
    dist_id = case.dist.id
    case.dist.remote_id = None
    old = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist_id))
    old.remote_write_attempted, old.status = True, 'failed'
    db.commit()
    if not found:
        force_remote.rows.clear()
    task = enqueue(case)
    assert worker.run_once()
    db.expire_all()
    assert db.get(Distribution, dist_id) is None and not force_remote.writes
    current = status(case, task)
    assert current['status'] == 'needs_review'
    assert ('发现同名' if found else '未找到同名') in current['items'][0]['error']
    assert retry(case, task).status_code == 200
    assert worker.run_once() and not force_remote.writes


def test_never_created_target_needs_no_remote_or_site_permission(db, delete_case, monkeypatch):
    case = delete_case
    case.dist.remote_id = None
    case.dist.status, case.dist.remote_snapshot = 'failed', {}
    for item in db.scalars(select(TaskItem).where(TaskItem.distribution_id == case.dist.id)):
        item.remote_write_attempted, item.status = False, 'failed'
    db.commit()
    task = enqueue(case)
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: pytest.fail('Never-created target needs no network'))
    assert worker.run_once()
    assert status(case, task)['result']['remote_confirmed'] is True


@pytest.mark.parametrize('evidence', ['snapshot', 'status'])
def test_missing_remote_id_with_old_observation_never_claims_absence(db, delete_case, force_remote, evidence):
    case = delete_case
    case.dist.remote_id = None
    if evidence == 'snapshot':
        case.dist.status = 'failed'
    else:
        case.dist.remote_snapshot = {}
        case.dist.status = 'missing'
    for item in db.scalars(select(TaskItem).where(TaskItem.distribution_id == case.dist.id)):
        item.remote_write_attempted, item.status = False, 'failed'
    db.commit()
    task = enqueue(case)
    assert worker.run_once()
    assert status(case, task)['status'] == 'needs_review'
    assert not force_remote.writes


def test_permission_failure_keeps_only_outbox_and_can_retry(db, delete_case, force_remote):
    dist_id = delete_case.dist.id
    task = enqueue(delete_case)
    force_remote.fail = 'read'
    assert worker.run_once()
    db.expire_all()
    assert db.get(Distribution, dist_id) is None and not force_remote.writes
    current = status(delete_case, task)
    assert current['can_retry'] is True and '本地已删除' in current['items'][0]['error']
    assert '本地记录已保留' not in current['items'][0]['error']
    force_remote.fail = None
    assert retry(delete_case, task).status_code == 200
    assert worker.run_once() and len(force_remote.writes) == 1


@pytest.mark.parametrize('change', ['name', 'type'])
def test_remote_identity_changed_stops_cleanup(db, delete_case, force_remote, change):
    remote_id = delete_case.dist.remote_id
    task = enqueue(delete_case)
    force_remote.rows[remote_id][change] = 'new-channel' if change == 'name' else 33
    assert worker.run_once()
    assert not force_remote.writes and status(delete_case, task)['status'] == 'needs_review'


@pytest.mark.parametrize('binding', ['distribution', 'historical_discovery_record'])
def test_new_local_binding_protects_remote_from_old_cleanup(db, delete_case, force_remote, binding):
    case = delete_case
    remote_id, site_id, name = case.dist.remote_id, case.dist.site_id, case.dist.remote_name
    task = enqueue(case)
    if binding == 'distribution':
        db.add(Distribution(channel_id=case.channel.id, site_id=site_id, remote_id=remote_id,
                            remote_name=name, partition_key='new-owner-binding', models=['test-model']))
    else:
        db.add(UnclaimedChannel(site_id=site_id, remote_id=remote_id, remote_name=name,
                                adopted_channel_id=case.channel.id, snapshot=deepcopy(force_remote.rows[remote_id])))
    db.commit()
    assert worker.run_once()
    assert not force_remote.writes
    assert '新的本地归属' in status(case, task)['items'][0]['error']


def test_old_remote_cleanup_reservation_blocks_new_create_readback_link(db, delete_case, force_remote):
    case = delete_case
    remote = deepcopy(force_remote.rows[case.dist.remote_id])
    enqueue(case)
    dist = Distribution(channel_id=case.channel.id, site_id=case.dist.site_id,
                        remote_name='new-local-channel', partition_key='new-upload-partition')
    db.add(dist)
    db.commit()
    with pytest.raises(worker.WriteStopped, match='等待清理'):
        worker.record_remote(db, dist, remote, 1)
    assert dist.remote_id is None


def test_pending_cleanup_blocks_unclaimed_discovery(db, users, delete_case, force_remote):
    case = delete_case
    site_id, remote_id = case.dist.site_id, case.dist.remote_id
    enqueue(case)
    task = SimpleNamespace(id='fixture-sync')
    item = SimpleNamespace(snapshot={'discover': True, 'owner_ids': []})
    adapter = SimpleNamespace(channels=lambda: list(force_remote.rows.values()))
    worker.sync_site(db, adapter, task, item, users['root'], db.get(Site, site_id))
    db.commit()
    assert db.scalar(select(UnclaimedChannel.id).where(UnclaimedChannel.site_id == site_id,
                                                      UnclaimedChannel.remote_id == remote_id)) is None


def test_actual_delete_holds_target_fence_through_transport(db, delete_case, force_remote, monkeypatch):
    from app.remote_cleanup import lock_target
    from fastapi import HTTPException

    original = worker.get_adapter
    observed = []
    def adapter(site, before_write=None):
        result = original(site, before_write=before_write)
        def delete(remote_id, *, expected):
            result.before_write()
            with SessionLocal() as competing:
                with pytest.raises(HTTPException) as blocked:
                    lock_target(competing, site.id, remote_id)
                assert blocked.value.status_code == 409
            observed.append(remote_id)
            force_remote.rows.pop(remote_id)
        result.delete = delete
        return result
    monkeypatch.setattr(worker, 'get_adapter', adapter)
    task = enqueue(delete_case)
    assert worker.run_once()
    assert len(observed) == 1 and status(delete_case, task)['status'] == 'succeeded'


@pytest.mark.parametrize('change', ['origin', 'seller', 'actor'])
def test_frozen_site_identity_and_actor_session_still_apply_after_local_purge(db, users, delete_case, force_remote, change):
    task = enqueue(delete_case)
    site = db.get(Site, task['items'][0]['site_id'])
    if change == 'origin':
        site.base_url = 'https://changed.invalid'
    elif change == 'seller':
        site.seller_user_id = '999999'
    else:
        users['user'].session_version += 1
    db.commit()
    assert worker.run_once() and not force_remote.writes
    item = db.get(TaskItem, task['items'][0]['id'])
    db.refresh(item)
    assert item.status == 'cancelled'
