"""A deleted queue/source cannot be restored by a paused worker or late read."""
# ruff: noqa: F811
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.silicon import RemoteError
from app.channel_purge import purge_distributions
from app.channel_service import get_channel
from app.db import SessionLocal, utcnow
from app.models import AuditEvent, Site, User
from app.models_channels import Channel, Distribution, Task, TaskItem, UnclaimedChannel
from sqlalchemy import select
from test_channel_monitoring import monitor_action, observed  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401


def purge(case):
    # Isolate the internal cleanup transaction from the paused worker. The
    # public force action now confirms remote deletion asynchronously; these
    # tests specifically exercise its physical-removal concurrency boundary.
    channel_id, dist_id = case.channel.id, case.dist.id
    with SessionLocal() as concurrent:
        selected = concurrent.get(Channel, channel_id)
        assert selected is not None
        actor = concurrent.scalar(select(User).where(User.id == selected.owner_id).with_for_update(key_share=True))
        channel = get_channel(concurrent, actor, channel_id, lock=True)
        result = purge_distributions(concurrent, actor, channel, [dist_id])
        concurrent.commit()
        assert result['deleted_distribution_ids'] == [dist_id]


def expire_and_purge(case, item_id):
    with SessionLocal() as concurrent:
        item = concurrent.get(TaskItem, item_id)
        item.lease_until = utcnow() - timedelta(seconds=1)
        concurrent.commit()
    purge(case)


def assert_work_gone(item_id, task_id, dist_id):
    with SessionLocal() as fresh:
        assert fresh.get(TaskItem, item_id) is None
        assert fresh.get(Task, task_id) is None
        assert fresh.get(Distribution, dist_id) is None
        assert not list(fresh.scalars(select(AuditEvent).where(AuditEvent.object_id == task_id)))
        assert not any(event.summary.get('item_id') == item_id for event in fresh.scalars(select(AuditEvent)))


@pytest.fixture
def closed_leases(monkeypatch, observed):
    closed = []
    original = worker.SiteLease

    class TrackedLease(original):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(worker, 'SiteLease', TrackedLease)
    return closed


@pytest.mark.parametrize('operation', ['test', 'sync_usage'])
def test_deleted_step_stops_before_any_provider_call(db, observed, operation):
    item, _ = monitor_action(db, observed, operation)
    ids = item.id, item.task_id, item.distribution_id
    purge(observed)
    with pytest.raises(worker.WorkRemoved):
        worker.execute_item(db, item)
    db.rollback()
    assert not observed.tests and not observed.reads
    assert_work_gone(*ids)


def test_stale_remote_record_cannot_flush_after_source_was_deleted(db, observed):
    item, _ = monitor_action(db, observed)
    ids = item.id, item.task_id, item.distribution_id
    stale = db.get(Distribution, item.distribution_id)
    purge(observed)
    stale.remote_snapshot = {'late_result': True}
    with pytest.raises(worker.WorkRemoved):
        worker.record_remote(db, stale, observed.remote)
    db.rollback()
    assert_work_gone(*ids)


@pytest.mark.parametrize('outcome', ['success', 'retryable', 'unknown', 'exception'])
def test_late_usage_response_does_not_recreate_queue_or_audit(db, observed, closed_leases, outcome):
    item, _ = monitor_action(db, observed, 'sync_usage')
    ids = item.id, item.task_id, item.distribution_id
    observed.on_usage = lambda: expire_and_purge(observed, ids[0])
    if outcome == 'exception':
        observed.usage_error = RuntimeError('private response must not persist')
    elif outcome != 'success':
        observed.usage_error = RemoteError('late response', retryable=outcome == 'retryable', unknown=outcome == 'unknown')
    assert worker.run_once(client=object())
    assert closed_leases == [True]
    assert_work_gone(*ids)


@pytest.mark.parametrize('outcome', ['success', 'retryable', 'exception'])
def test_finalizer_discards_dirty_cached_entities_after_harddelete(db, observed, closed_leases, monkeypatch, outcome):
    item, _ = monitor_action(db, observed)
    ids = item.id, item.task_id, item.distribution_id

    def abandoned_operation(session, running, lease_check):
        stale = session.get(Distribution, running.distribution_id)
        expire_and_purge(observed, ids[0])
        running.snapshot = {**running.snapshot, 'operation_result': {'success': True}}
        stale.remote_snapshot = {'_monitoring': {'connectivity_test': {'status': 'succeeded'}}}
        if outcome == 'retryable':
            raise RemoteError('late response', retryable=True)
        if outcome == 'exception':
            raise RuntimeError('private response must not persist')

    monkeypatch.setattr(worker, 'execute_item', abandoned_operation)
    assert worker.run_once(client=object())
    assert closed_leases == [True]
    assert_work_gone(*ids)


def test_purge_during_preflight_stops_model_test_before_write(db, observed, closed_leases):
    item, _ = monitor_action(db, observed)
    ids = item.id, item.task_id, item.distribution_id
    observed.before_test = lambda: expire_and_purge(observed, ids[0])
    assert worker.run_once(client=object())
    assert not observed.tests
    assert closed_leases == [True]
    assert_work_gone(*ids)


def test_purge_between_write_stage_commit_and_final_lock_stops_http(db, observed, closed_leases, monkeypatch):
    item, _ = monitor_action(db, observed)
    ids = item.id, item.task_id, item.distribution_id
    original = worker.lock_result_distribution
    pending = True

    def purge_before_lock(session, running, **kwargs):
        nonlocal pending
        if pending:
            pending = False
            expire_and_purge(observed, ids[0])
        return original(session, running, **kwargs)

    monkeypatch.setattr(worker, 'lock_result_distribution', purge_before_lock)
    assert worker.run_once(client=object())
    assert not observed.tests
    assert closed_leases == [True]
    assert_work_gone(*ids)


def test_sync_skips_row_deleted_after_list_read_and_can_discover_remote(db, users, observed, monkeypatch):
    dist_id = observed.dist.id
    original = db.scalar
    pending = True

    def delete_before_distribution_lock(statement, *args, **kwargs):
        nonlocal pending
        if (pending and getattr(statement, '_for_update_arg', None) is not None
                and any(column.get('entity') is Distribution for column in statement.column_descriptions)):
            pending = False
            purge(observed)
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(db, 'scalar', delete_before_distribution_lock)
    task = db.scalar(select(Task))
    item = SimpleNamespace(snapshot={'owner_ids': [users['user'].id], 'discover': True})
    site = db.get(Site, observed.dist.site_id)
    adapter = SimpleNamespace(channels=lambda: [observed.remote])
    worker.sync_site(db, adapter, task, item, users['root'], site)
    db.commit()
    with SessionLocal() as fresh:
        assert fresh.get(Distribution, dist_id) is None
        found = fresh.scalar(select(UnclaimedChannel).where(UnclaimedChannel.site_id == site.id,
                                                            UnclaimedChannel.remote_id == str(observed.remote['id'])))
        assert found is not None and found.adopted_channel_id is None
