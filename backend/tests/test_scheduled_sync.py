"""Automatic synchronization: isolated PostgreSQL and fake remote/lease services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.channel_observation import quota_conversion
from app.adapters.silicon import RemoteError
from app.config import settings
from app.db import SessionLocal, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_channels import Channel, Distribution, Task, TaskItem, UploadGroup
from app.security import encrypt
from sqlalchemy import func, select


def site(db, name='automatic', **flags):
    row = Site(name=name, prefix=name, base_url=f'https://{name}.invalid',
               seller_user_id='1', token_encrypted=encrypt('test-only'), **flags)
    db.add(row)
    db.commit()
    return row


def batches(db):
    db.expire_all()
    return list(db.scalars(select(Task).where(Task.kind == 'scheduled_sync').order_by(Task.created_at)))


class Lease:
    def __init__(self, *_):
        pass

    def acquire(self):
        return True

    def check(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize(('configured', 'expected'), [(0, 300), (-20, 300), (300, 300), (900, 900)])
def test_sync_interval_nonpositive_values_use_default(monkeypatch, configured, expected):
    monkeypatch.setattr(settings, 'sync_interval_seconds', configured)
    assert worker.sync_interval_seconds() == expected


def test_schedule_covers_collecting_sites_and_all_accounts(db, users):
    active = site(db, 'active', enabled=True)
    paused_writes = site(db, 'paused-writes', enabled=False)
    site(db, 'archived', archived=True)
    site(db, 'no-collection', collect_enabled=False)
    users['user'].active = False
    users['other_user'].archived = True
    db.commit()

    worker.schedule_sync()
    [task] = batches(db)
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id)))
    assert {item.site_id for item in items} == {active.id, paused_writes.id}
    assert set(task.snapshot['owner_ids']) == {user.id for user in users.values()}
    for item in items:
        assert item.operation == 'sync'
        assert set(item.snapshot['owner_ids']) == set(task.snapshot['owner_ids'])
        assert item.snapshot['discover'] is True
        assert item.channel_id is None and item.distribution_id is None


def test_scheduler_waits_for_active_batch_and_interval_then_resumes(db, users, monkeypatch):
    site(db)
    now = utcnow()
    clock = [now]
    monkeypatch.setattr(worker, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(settings, 'sync_interval_seconds', 0)
    worker.schedule_sync()
    [task] = batches(db)
    task.created_at = now
    task.status = 'succeeded'
    db.commit()

    clock[0] = now + timedelta(seconds=299)
    worker.schedule_sync()
    assert len(batches(db)) == 1  # An invalid setting still keeps the 300-second interval.
    clock[0] = now + timedelta(seconds=301)
    task.status = 'running'
    db.commit()
    worker.schedule_sync()
    assert len(batches(db)) == 1  # Long batches never overlap their next interval.
    task.status = 'failed'
    db.commit()
    worker.schedule_sync()
    assert len(batches(db)) == 2  # Terminal failures do not permanently stop the scheduler.


def test_schedule_requires_an_available_superadmin_and_site(db, users):
    worker.schedule_sync()
    assert not batches(db)
    site(db)
    users['root'].active = False
    db.commit()
    worker.schedule_sync()
    assert not batches(db)


def test_database_scheduler_lock_fences_expired_redis_lease(db, users):
    if db.get_bind().dialect.name != 'postgresql':
        pytest.skip('Transaction advisory lock requires PostgreSQL')
    site(db)
    with SessionLocal() as first_scheduler:
        assert first_scheduler.scalar(select(func.pg_try_advisory_xact_lock(worker._SCHEDULER_LOCK)))
        worker.schedule_sync()  # A second scheduler cannot publish during the first transaction.
        assert not batches(db)
        first_scheduler.rollback()
    worker.schedule_sync()
    assert len(batches(db)) == 1


def test_concurrent_schedulers_publish_one_batch(db, users):
    if db.get_bind().dialect.name != 'postgresql':
        pytest.skip('Concurrent scheduling is verified on PostgreSQL')
    site(db)
    barrier = Barrier(2)

    def schedule():
        barrier.wait(timeout=5)
        worker.schedule_sync()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(schedule) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(batches(db)) == 1
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 1


def test_automatic_sync_reads_channels_for_every_account_including_archived(db, users, monkeypatch):
    target = site(db, enabled=False)
    category = Category(name='Scheduled fixture', family='OpenAI')
    db.add(category)
    db.flush()
    fmt = CredentialFormat(category_id=category.id, code='scheduled-fixture', name='Fixture')
    db.add(fmt)
    db.flush()
    distributions, records = [], []
    for index, owner in enumerate(users.values(), start=1):
        group = UploadGroup(owner_id=owner.id, category_id=category.id, format_id=fmt.id, tag=f'scheduled-{index}')
        db.add(group)
        db.flush()
        channel = Channel(owner_id=owner.id, group_id=group.id, category_id=category.id,
                          format_id=fmt.id, key_encrypted=encrypt('test-only'), key_hint='fixture',
                          fingerprint=f'scheduled-{index}', archived=index % 2 == 0)
        db.add(channel)
        db.flush()
        dist = Distribution(channel_id=channel.id, site_id=target.id, remote_id=str(index),
                            remote_name=f'fixture-{index}')
        db.add(dist)
        distributions.append(dist)
        records.append({'id': index, 'name': dist.remote_name, 'type': 1, 'status': 2,
                        'models': 'fixture', 'group': 'default', 'used_quota': index * 500000, 'balance': 0})
    records[-1].pop('used_quota')  # A successful read with no counter is still unknown consumption.
    users['user'].active = False
    users['other_user'].archived = True
    db.commit()
    conversion = quota_conversion({'version': 'v1.0.0-rc.25-fix-36', 'quota_per_unit': 500000},
                                  adapter_kind='silicon-v1', verified_version='v1.0.0-rc.25-fix-36')
    reads = []

    def channels():
        reads.append('channels')
        return records

    monkeypatch.setattr(worker, 'get_adapter', lambda *_args, **_kwargs: SimpleNamespace(
        channels=channels, usage_conversion=lambda **_: conversion))
    monkeypatch.setattr(worker, 'SiteLease', Lease)
    worker.schedule_sync()
    assert worker.run_once(client=object())
    assert batches(db)[0].status == 'succeeded'
    assert reads == ['channels']
    for index, dist in enumerate(distributions, start=1):
        db.refresh(dist)
        observation = dist.remote_snapshot['_monitoring']['usage_sync']
        assert observation['status'] == 'succeeded'
        assert observation['remote_usage']['used_amount'] == (str(index) if index < len(distributions) else None)
        assert observation['synced_at'] and dist.last_sync_at


@pytest.mark.parametrize('eventual_success', [True, False])
def test_retryable_read_failures_retry_then_recover_next_period(db, users, monkeypatch, eventual_success):
    target = site(db)
    clock = [utcnow()]
    monkeypatch.setattr(worker, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(worker, 'SiteLease', Lease)
    attempts = []

    def channels():
        attempts.append(clock[0])
        if not eventual_success or len(attempts) == 1:
            raise RemoteError('simulated temporary failure', retryable=True, category='connection_error')
        return []

    monkeypatch.setattr(worker, 'get_adapter', lambda *_args, **_kwargs: SimpleNamespace(channels=channels))
    worker.schedule_sync()
    [task] = batches(db)
    original_id = task.id
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == original_id))
    assert item.status == 'pending' and item.attempts == 1
    assert item.next_attempt_at == clock[0] + timedelta(seconds=10)
    assert not worker.run_once(client=object())
    worker.schedule_sync()
    assert len(batches(db)) == 1
    assert db.get(Site, target.id).last_sync_at is None

    clock[0] = item.next_attempt_at
    assert worker.run_once(client=object())
    db.refresh(item)
    if eventual_success:
        assert item.status == 'succeeded' and item.attempts == 2
        assert db.get(Site, target.id, populate_existing=True).last_sync_at == clock[0]
    else:
        assert item.status == 'pending' and item.attempts == 2
        assert item.next_attempt_at == clock[0] + timedelta(seconds=20)
        clock[0] = item.next_attempt_at
        assert worker.run_once(client=object())
        db.refresh(item)
        assert item.status == 'failed' and item.attempts == 3
        assert not worker.run_once(client=object())
        assert db.get(Site, target.id, populate_existing=True).last_sync_at is None
        clock[0] += timedelta(seconds=301)
        worker.schedule_sync()
        assert len(batches(db)) == 2
        assert len(attempts) == 3


def test_failed_scheduled_read_preserves_last_successful_site_metadata(db, users, monkeypatch):
    target = site(db)
    target.last_sync_at = utcnow() - timedelta(minutes=10)
    target.capabilities = {'usage_conversion': {'existing': 'evidence'}}
    db.commit()
    previous = deepcopy(target.capabilities), target.last_sync_at
    monkeypatch.setattr(worker, 'SiteLease', Lease)

    def channels():
        raise RemoteError('simulated unavailable site', retryable=True, category='connection_error')

    monkeypatch.setattr(worker, 'get_adapter', lambda *_args, **_kwargs: SimpleNamespace(channels=channels))
    worker.schedule_sync()
    assert worker.run_once(client=object())
    db.refresh(target)
    assert (target.capabilities, target.last_sync_at) == previous
