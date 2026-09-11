"""Only scheduled synchronization may create new reads; old task evidence survives."""
# ruff: noqa: F811 - pytest injects the imported shared fixture.
from copy import deepcopy

import pytest
from app import worker
from app.channel_service import new_task
from app.distribution_monitoring import observation_json
from app.models import Site
from app.models_billing import UsageFact
from app.models_channels import Distribution, Task, TaskItem
from app.routers.channels import existing_operation
from app.security import encrypt
from sqlalchemy import func, select
from test_channel_monitoring import observed  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401


def snapshot(row):
    return deepcopy({column.name: getattr(row, column.name) for column in row.__table__.columns})


@pytest.fixture
def sync_site(db):
    site = Site(name='Background synchronization', prefix='background-sync',
                base_url='https://background-sync.invalid', seller_user_id='1',
                token_encrypted=encrypt('fixture-only'), collect_enabled=True)
    db.add(site)
    db.commit()
    return site


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_manual_channel_usage_action_is_rejected_without_changing_tasks(db, login, observed, role):
    client = login(role)
    task_count = db.scalar(select(func.count()).select_from(Task))
    item_count = db.scalar(select(func.count()).select_from(TaskItem))
    previous = snapshot(observed.dist)
    result = client.post('/api/channels/' + observed.channel.id + '/actions', json={
        'action': 'sync_usage', 'distribution_ids': [observed.dist.id],
    })
    assert result.status_code == 422
    assert any(error['loc'][-1] == 'action' for error in result.json()['detail'])
    assert db.scalar(select(func.count()).select_from(Task)) == task_count
    assert db.scalar(select(func.count()).select_from(TaskItem)) == item_count
    db.refresh(observed.dist)
    assert snapshot(observed.dist) == previous
    assert not observed.reads


@pytest.mark.parametrize(('kind', 'operation'), [
    ('sync', 'sync'), ('scheduled_sync', 'sync'), ('sync_usage', 'sync_usage'),
    ('legacy_batch', 'sync'), ('legacy_batch', 'sync_usage'),
    ('sync_usage', 'legacy_observation'),
])
@pytest.mark.parametrize(('endpoint', 'status'), [('retry', 'failed'), ('reconcile', 'needs_review')])
def test_history_remains_readable_but_cannot_resubmit_sync(
        db, users, login, monkeypatch, sync_site, kind, operation, endpoint, status):
    notified = []
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: notified.append(True))
    task = new_task(db, users['user'], users['user'].id, kind,
                    snapshot={'owner_ids': [users['user'].id]})
    task.status = status
    item = TaskItem(task_id=task.id, site_id=sync_site.id, operation=operation,
                    status=status, attempts=2, error='Saved synchronization failure',
                    snapshot={'owner_ids': [users['user'].id], 'operation_result': {
                        'remote_usage': {'used_quota': 0}, 'synced_at': '2026-09-10T00:00:00Z'}})
    db.add(item)
    db.commit()
    before_task, before_item = snapshot(task), snapshot(item)
    client = login('user')
    detail = client.get('/api/tasks/' + task.id)
    assert detail.status_code == 200 and detail.json()['kind'] == kind
    assert login('other_user').get('/api/tasks/' + task.id).status_code == 404
    response = client.post('/api/tasks/' + task.id + '/' + endpoint)
    assert response.status_code == 409
    assert '后台定时' in response.json()['detail']
    db.refresh(task)
    db.refresh(item)
    assert snapshot(task) == before_task
    assert snapshot(item) == before_item
    assert not notified


@pytest.mark.parametrize(('endpoint', 'status', 'stage', 'written'), [
    ('retry', 'failed', 'queued', False),
    ('reconcile', 'needs_review', 'updated_pending_verification', True),
])
def test_non_sync_task_resubmission_is_unchanged(
        db, users, monkeypatch, observed, endpoint, status, stage, written):
    notified = []
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: notified.append(True))
    task = existing_operation(db, users['user'], observed.channel, 'disable', [observed.dist])
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == task.id))
    task.status = item.status = status
    item.stage, item.remote_write_attempted, item.attempts = stage, written, 2
    db.commit()
    result = observed.client.post('/api/tasks/' + task.id + '/' + endpoint)
    assert result.status_code == 200, result.text
    db.refresh(task)
    db.refresh(item)
    assert task.status == 'queued' and item.status == 'pending'
    assert item.operation == 'disable' and item.remote_write_attempted is written
    assert item.stage == stage
    assert item.attempts == (0 if endpoint == 'retry' else 2)
    assert notified == [True]


def test_already_queued_channel_usage_sync_still_executes_and_preserves_evidence(db, users, observed):
    task = existing_operation(db, users['user'], observed.channel, 'sync_usage', [observed.dist])
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == task.id))
    db.commit()
    assert worker.run_once(client=object())
    db.refresh(task)
    db.refresh(item)
    dist = db.get(Distribution, observed.dist.id)
    db.refresh(dist)
    saved = observation_json(dist, 'sync_usage')
    assert task.status == 'succeeded' and item.status == 'succeeded'
    assert observed.reads == [observed.dist.remote_id]
    assert saved['task_id'] == task.id and saved['remote_usage'] == observed.usage_value
    assert saved['remote_usage']['used_quota'] == 0
    assert item.snapshot['operation_result']['synced_at'] == saved['synced_at']
    assert observed.client.get('/api/tasks/' + task.id).status_code == 200
    assert db.scalar(select(func.count()).select_from(UsageFact)) == 0
