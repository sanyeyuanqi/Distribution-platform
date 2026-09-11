"""Recreated sites share remote ownership fences while keeping local history separate."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app import remote_cleanup, worker
from app.adapters.silicon import RemoteError
from app.db import SessionLocal
from app.models import Category, CredentialFormat, Site
from app.models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)
from app.remote_channel_names import new_remote_channel_name
from fastapi import HTTPException
from sqlalchemy import select, text


def row_snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def remote(remote_id, **values):
    return {'id': str(remote_id), 'name': f'remote-{remote_id}', 'type': 1,
            'models': 'current-model', 'group': 'default', 'status': 1, **values}


@pytest.fixture
def recreated(db, users):
    connection = {'name': '重建站点', 'prefix': 'recreated', 'base_url': 'https://recreated.invalid',
                  'seller_user_id': '71', 'adapter': 'silicon-v1', 'token_encrypted': 'fixture-ciphertext'}
    old = Site(**connection, archived=True, enabled=False)
    current = Site(**connection, enabled=True)
    category = Category(name='Remote identity fixtures', family='OpenAI')
    db.add_all([old, current, category])
    db.flush()
    fmt = CredentialFormat(category_id=category.id, code='remote-identity', name='Fixture API key')
    db.add(fmt)
    db.flush()
    group = UploadGroup(owner_id=users['user'].id, category_id=category.id, format_id=fmt.id,
                        tag='original-immutable-upload-tag')
    db.add(group)
    db.flush()
    channel = Channel(owner_id=users['user'].id, group_id=group.id, category_id=category.id,
                      format_id=fmt.id, key_encrypted='fixture-key-ciphertext', key_hint='masked',
                      fingerprint='remote-identity-fixture-fingerprint')
    db.add(channel)
    db.flush()
    base = f'{current.name}-{group.tag}'
    historical = Distribution(channel_id=channel.id, site_id=old.id, remote_id='101', remote_name=base,
                              key_version=3, status='disabled', models=['historical-model'],
                              remote_snapshot={'id': '101', 'used_quota': 900, 'historical': True},
                              error='Historical observation')
    pending = Distribution(channel_id=channel.id, site_id=current.id, remote_name=base + '-2')
    db.add_all([historical, pending])
    db.flush()
    version = DistributionVersion(distribution_id=historical.id, key_version=3)
    db.add(version)
    db.commit()
    return SimpleNamespace(old=old, current=current, historical=historical, pending=pending,
                           version=version, channel=channel, group=group, base=base, actor=users['root'])


def cleanup_item(db, case, remote_id, *, state='pending', name=None, acknowledged=False):
    task = Task(actor_id=case.actor.id, owner_id=case.channel.owner_id,
                actor_session_version=case.actor.session_version, kind='force_delete_async', status=state)
    db.add(task)
    db.flush()
    snapshot = {'delete_target': {'id': str(remote_id), 'name': name or f'remote-{remote_id}', 'type': 1}}
    if acknowledged:
        snapshot['delete_acknowledged'] = True
    item = TaskItem(task_id=task.id, site_id=case.old.id, operation='remote_cleanup', status=state,
                    snapshot=snapshot)
    db.add(item)
    db.flush()
    return item


@pytest.mark.parametrize('state', ['pending', 'failed'])
def test_new_names_reserve_archived_distribution_unclaimed_and_cleanup_names(db, recreated, state):
    case = recreated
    # The new pending distribution does not occupy an ordinal in this example.
    case.pending.remote_name = 'unrelated-current-name'
    case.historical.status = 'deleted'
    db.add(UnclaimedChannel(site_id=case.old.id, remote_id='201', remote_name=case.base + '-2', snapshot={}))
    cleanup = cleanup_item(db, case, '202', state=state, name=case.base + '-3')
    db.commit()
    original = row_snapshot(case.historical)

    assert case.old.id != case.current.id and case.old.archived
    assert new_remote_channel_name(db, case.current, case.channel) == case.base + '-4'
    cleanup.snapshot = {**cleanup.snapshot, 'delete_acknowledged': True}
    db.commit()
    assert new_remote_channel_name(db, case.current, case.channel) == case.base + '-3'
    assert row_snapshot(case.historical) == original


def test_first_remote_binding_rejects_historical_id_without_mutation_but_accepts_new_id(db, recreated):
    case = recreated
    original = row_snapshot(case.historical)
    original_version = row_snapshot(case.version)
    pending = row_snapshot(case.pending)

    with pytest.raises(worker.WriteStopped, match='历史本地关联'):
        worker.record_remote(db, case.pending, remote('101', used_quota=1200), key_version=4)
    db.commit()
    db.refresh(case.historical)
    db.refresh(case.pending)
    db.refresh(case.version)
    assert row_snapshot(case.historical) == original
    assert row_snapshot(case.version) == original_version
    assert row_snapshot(case.pending) == pending
    assert db.scalar(select(DistributionVersion.id).where(
        DistributionVersion.distribution_id == case.pending.id)) is None

    worker.record_remote(db, case.pending, remote('102', used_quota=7), key_version=4)
    db.commit()
    db.refresh(case.pending)
    assert case.pending.remote_id == '102' and case.pending.status == 'enabled'
    assert case.pending.key_version == 4 and case.pending.remote_snapshot['id'] == '102'
    assert list(db.scalars(select(DistributionVersion.key_version).where(
        DistributionVersion.distribution_id == case.pending.id))) == [4]
    db.refresh(case.historical)
    db.refresh(case.version)
    assert row_snapshot(case.historical) == original
    assert row_snapshot(case.version) == original_version


@pytest.mark.parametrize('state', ['pending', 'failed'])
def test_archived_cleanup_reserves_id_against_new_binding_until_acknowledged(db, recreated, state):
    case = recreated
    cleanup = cleanup_item(db, case, '201', state=state)
    db.commit()
    assert remote_cleanup.is_reserved(db, case.current.id, '201')
    with pytest.raises(worker.WriteStopped, match='等待清理'):
        worker.record_remote(db, case.pending, remote('201'), key_version=1)
    db.commit()
    db.refresh(case.pending)
    assert case.pending.remote_id is None and case.pending.remote_snapshot == {}

    cleanup.snapshot = {**cleanup.snapshot, 'delete_acknowledged': True}
    db.commit()
    assert not remote_cleanup.is_reserved(db, case.current.id, '201')
    worker.record_remote(db, case.pending, remote('201'), key_version=1)
    db.commit()
    assert case.pending.remote_id == '201'


def test_discovery_skips_historical_bindings_and_pending_or_failed_cleanup(db, recreated):
    case = recreated
    cleanup_item(db, case, '201', state='pending')
    cleanup_item(db, case, '202', state='failed')
    cleanup_item(db, case, '203', state='succeeded', acknowledged=True)
    db.commit()
    original = row_snapshot(case.historical)
    original_version = row_snapshot(case.version)
    task = SimpleNamespace(id='fixture-discovery')
    item = SimpleNamespace(snapshot={'discover': True, 'owner_ids': [case.channel.owner_id]})
    adapter = SimpleNamespace(channels=lambda: [remote(remote_id, used_quota=1200)
                                               for remote_id in ('101', '201', '202', '203', '301')])

    worker.sync_site(db, adapter, task, item, case.actor, case.current)
    db.commit()
    assert set(db.scalars(select(UnclaimedChannel.remote_id).where(
        UnclaimedChannel.site_id == case.current.id))) == {'203', '301'}
    db.refresh(case.historical)
    db.refresh(case.version)
    assert row_snapshot(case.historical) == original
    assert row_snapshot(case.version) == original_version
    assert case.pending.remote_id is None


@pytest.mark.parametrize('discover', [False, True])
def test_normal_sync_updates_only_current_site_distributions(db, recreated, discover):
    case = recreated
    case.pending.remote_id, case.pending.status = '102', 'disabled'
    db.commit()
    original = row_snapshot(case.historical)
    original_version = row_snapshot(case.version)
    task = SimpleNamespace(id='fixture-current-site-sync')
    item = SimpleNamespace(snapshot={'discover': discover, 'owner_ids': [case.channel.owner_id]})
    adapter = SimpleNamespace(channels=lambda: [remote('101', used_quota=1200), remote('102', used_quota=7)])

    worker.sync_site(db, adapter, task, item, case.actor, case.current)
    db.commit()
    db.refresh(case.pending)
    from app.distribution_monitoring import observation_json
    assert case.pending.status == 'enabled'
    assert observation_json(case.pending, 'sync_usage')['remote_usage']['used_quota'] == 7
    assert case.pending.last_sync_at is not None
    db.refresh(case.historical)
    db.refresh(case.version)
    assert row_snapshot(case.historical) == original
    assert row_snapshot(case.version) == original_version
    assert db.scalar(select(UnclaimedChannel.id)) is None


@pytest.mark.parametrize('binding', ['distribution', 'adopted_unclaimed'])
def test_old_cleanup_stops_when_recreated_site_has_new_local_ownership(db, recreated, binding):
    case = recreated
    cleanup = cleanup_item(db, case, '401', state='failed')
    if binding == 'distribution':
        case.pending.remote_id = '401'
    else:
        db.add(UnclaimedChannel(site_id=case.current.id, remote_id='401', remote_name='new-owner',
                                adopted_channel_id=case.channel.id, snapshot=remote('401')))
    db.commit()
    original = row_snapshot(cleanup)

    remote_cleanup.lock_target(db, case.old.id, '401')
    with pytest.raises(RemoteError, match='新的本地归属') as stopped:
        remote_cleanup.assert_unclaimed_target(db, case.old.id, '401')
    assert stopped.value.unknown is True
    assert row_snapshot(cleanup) == original


@pytest.mark.parametrize('identity_change', ['seller_user_id', 'adapter'])
def test_different_seller_or_adapter_has_independent_remote_namespace(db, recreated, identity_change):
    case = recreated
    setattr(case.current, identity_change, 'different-seller' if identity_change == 'seller_user_id' else 'newapi-v1')
    case.pending.remote_name = 'unrelated-current-name'
    cleanup_item(db, case, '201', state='failed', name=case.base + '-2')
    db.commit()
    original = row_snapshot(case.historical)

    assert new_remote_channel_name(db, case.current, case.channel) == case.base
    assert not remote_cleanup.is_reserved(db, case.current.id, '201')
    remote_cleanup.assert_unclaimed_target(db, case.current.id, '101')
    worker.record_remote(db, case.pending, remote('101'), key_version=1)
    db.commit()
    assert case.pending.remote_id == '101'
    task = SimpleNamespace(id='fixture-independent-discovery')
    item = SimpleNamespace(snapshot={'discover': True, 'owner_ids': []})
    worker.sync_site(db, SimpleNamespace(channels=lambda: [remote('201')]), task, item, case.actor, case.current)
    db.commit()
    assert list(db.scalars(select(UnclaimedChannel.remote_id).where(
        UnclaimedChannel.site_id == case.current.id))) == ['201']
    db.refresh(case.historical)
    assert row_snapshot(case.historical) == original


@pytest.mark.parametrize('holder_is_old', [True, False])
def test_target_lock_is_immediate_and_shared_across_old_and_new_site_ids(db, recreated, holder_is_old):
    case = recreated
    holder_id, competing_id = ((case.old.id, case.current.id) if holder_is_old
                               else (case.current.id, case.old.id))
    with SessionLocal() as holder, SessionLocal() as competing:
        remote_cleanup.lock_target(holder, holder_id, '501')
        # A regression to a waiting lock must fail promptly rather than hang.
        competing.execute(text("SET LOCAL statement_timeout = '1s'"))
        with pytest.raises(HTTPException) as blocked:
            remote_cleanup.lock_target(competing, competing_id, '501')
        assert blocked.value.status_code == 409
        remote_cleanup.lock_target(competing, competing_id, '502')
        holder.rollback()
        remote_cleanup.lock_target(competing, competing_id, '501')


@pytest.mark.parametrize('identity_change', ['seller_user_id', 'adapter'])
def test_target_locks_are_independent_for_different_seller_or_adapter(db, recreated, identity_change):
    case = recreated
    setattr(case.current, identity_change, 'different-seller' if identity_change == 'seller_user_id' else 'newapi-v1')
    db.commit()
    with SessionLocal() as holder, SessionLocal() as competing:
        remote_cleanup.lock_target(holder, case.old.id, '501')
        competing.execute(text("SET LOCAL statement_timeout = '1s'"))
        remote_cleanup.lock_target(competing, case.current.id, '501')
