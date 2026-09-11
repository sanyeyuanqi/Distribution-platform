"""The internal purge engine remains scoped; the public force API verifies remote deletion first."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from app.channel_purge import purge_distributions, references
from app.channel_service import api_json, get_channel
from app.db import SessionLocal, uid, utcnow
from app.deletion_locks import lock_billing_snapshot
from app.models import AuditEvent, User
from app.models_billing import SettlementOrder, SettlementOrderLine, UsageFact
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)
from fastapi import HTTPException
from settlement_fixtures import ledger, order_for  # noqa: F401
from sqlalchemy import func, select
from sqlalchemy.orm import object_session
from test_catalog_policy import catalog_setup  # noqa: F401
from test_channels import delete_case, payload, setup_catalog  # noqa: F401
from test_multikey_containers import (  # noqa: F401
    AWS_KEYS,
    container_site,
    post,
    request_body,
    setup_template,
)


def force(case, *, ids=None, client=None, **extra):
    ids = [case.dist.id] if ids is None else ids
    if client is not None or extra:
        return (client or case.client).post('/api/channels/' + case.channel.id + '/actions', json={
            'action': 'force_delete', 'distribution_ids': ids,
            'confirmation': f'DELETE REMOTE AND LOCAL {len(ids)}', **extra})
    # Exercise the cleanup engine separately from the now-asynchronous public
    # workflow; remote authorization/confirmation is covered in its own suite.
    from app.routers.channels import channel_json
    db = object_session(case.channel)
    actor = db.get(User, case.channel.owner_id)
    channel = get_channel(db, actor, case.channel.id, lock=True)
    try:
        result = purge_distributions(db, actor, channel, ids)
        db.commit()
        return httpx.Response(200, json=api_json({**result,
            'channel': None if result['channel_deleted'] else channel_json(db, channel)}))
    except HTTPException as exc:
        db.rollback()
        return httpx.Response(exc.status_code, json={'detail': exc.detail})


def fact_for(db, users, channel, dist):
    fact = UsageFact(source_id=uid(), site_id=dist.site_id, distribution_id=dist.id, channel_id=channel.id,
        owner_id=channel.owner_id, admin_id=users['admin'].id, category_id=channel.category_id,
        occurred_at=utcnow(), raw_amount=8, raw_unit='USD', amount=8, unit='USD',
        conversion_version='fixture', verified=True)
    db.add(fact)
    db.flush()
    return fact





@pytest.mark.parametrize('role', ['other_user', 'other_admin', 'sibling'])
def test_hard_delete_preserves_owner_and_team_permissions(db, login, delete_case, role):
    assert force(delete_case, client=login(role)).status_code == 404
    assert db.get(Distribution, delete_case.dist.id) is not None
    assert not delete_case.writes


@pytest.mark.parametrize('extra', [
    {'distribution_ids': None}, {'distribution_ids': []}, {'confirmation': None},
    {'confirmation': 'FORCE DELETE 1'}, {'confirmation': 'DELETE 1'},
    {'site_ids': ['any']}, {'model': 'test-model'}, {'confirmation': 'HARD DELETE 1'},
    {'distribution_ids': ['unknown']},
])
def test_hard_delete_requires_new_exact_confirmation_and_scope(db, delete_case, extra):
    assert force(delete_case, **extra).status_code == 422
    assert db.get(Distribution, delete_case.dist.id) is not None
    assert not delete_case.writes


def test_duplicate_and_cross_channel_targets_are_rejected_atomically(db, delete_case, setup_catalog):
    case = delete_case
    assert force(case, ids=[case.dist.id, case.dist.id]).status_code == 422
    other = case.client.post('/api/uploads/submit', json=payload(setup_catalog, 'other-fixture-key', 'other-request')).json()
    other_id = other['items'][0]['distribution_id']
    assert force(case, ids=[case.dist.id, other_id]).status_code == 422
    assert db.scalar(select(func.count()).select_from(Distribution)) == 6


@pytest.mark.parametrize('state', ['missing', 'remote_deleted', 'legacy_tombstone', 'no_remote'])
def test_existing_removed_or_archived_rows_can_be_purged_without_site_access(db, login, delete_case, state):
    from app.models import Site
    case = delete_case
    old_id = case.dist.id
    case.channel.archived = True
    site = db.get(Site, case.dist.site_id)
    site.enabled, site.archived, site.verified_at, site.capabilities = False, True, None, {}
    if state in ('remote_deleted', 'legacy_tombstone'):
        case.dist.status = 'deleted'
    if state == 'legacy_tombstone':
        case.dist.remote_snapshot = {'_local_deletion': {'remote_confirmed': False}}
    if state == 'no_remote':
        case.dist.remote_id = None
    db.commit()
    result = force(case)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['deleted_distribution_ids'] == [old_id] and data['remote_confirmed'] is False
    assert not data['channel_deleted'] and data['deleted_channel_id'] is None
    assert len(data['channel']['distributions']) == 2
    db.expire_all()
    assert db.get(Distribution, old_id) is None
    assert not case.writes and case.detail_reads == 0


def test_partial_delete_removes_related_history_usage_and_steps_only(db, users, delete_case):
    case = delete_case
    selected_id, channel_id = case.dist.id, case.channel.id
    other = db.scalar(select(Distribution).where(Distribution.channel_id == channel_id, Distribution.id != selected_id))
    selected_fact = fact_for(db, users, case.channel, case.dist)
    other_fact = fact_for(db, users, case.channel, other)
    version = DistributionVersion(distribution_id=selected_id, key_version=1)
    db.add(version)
    db.commit()
    selected_fact_id, version_id = selected_fact.id, version.id
    remaining = [(item.id, deepcopy(item.snapshot)) for item in db.scalars(select(TaskItem).where(TaskItem.distribution_id != selected_id))]
    original_ciphertext = case.channel.key_encrypted
    result = force(case)
    assert result.status_code == 200, result.text
    db.expire_all()
    assert db.get(Distribution, selected_id) is None
    for model, row_id in ((UsageFact, selected_fact_id), (DistributionVersion, version_id)):
        assert db.get(model, row_id) is None
    assert db.get(UsageFact, other_fact.id).amount == 8
    assert db.get(Channel, channel_id).key_encrypted == original_ciphertext
    assert db.scalar(select(func.count()).select_from(KeyVersion)) == 1
    assert all(db.get(TaskItem, item_id).snapshot == snap for item_id, snap in remaining)
    task = db.scalar(select(Task))
    assert len(task.snapshot['site_ids']) == 2
    assert result.json()['channel']['usage_by_unit'] == {'USD': '8.00000000'}


def test_last_distribution_removes_channel_credentials_empty_task_and_group(db, delete_case, setup_catalog):
    case = delete_case
    channel_id, group_id = case.channel.id, case.channel.group_id
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == channel_id)))
    item_ids = set(db.scalars(select(TaskItem.id)))
    task_ids = set(db.scalars(select(Task.id)))
    result = force(case, ids=ids)
    assert result.status_code == 200, result.text
    assert result.json()['channel_deleted'] and result.json()['deleted_channel_id'] == channel_id
    assert result.json()['channel'] is None
    db.expire_all()
    for model in (Channel, ChannelCredential, KeyVersion, Distribution, TaskItem, Task, UploadGroup):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    removed = {channel_id, group_id, *ids, *item_ids, *task_ids}
    events = list(db.scalars(select(AuditEvent)))
    assert all(event.object_id not in removed and not references(event.summary, removed) for event in events)
    purge_audit = next(event for event in events if event.action == 'channel.hard_delete_local')
    assert purge_audit.object_id is None
    assert purge_audit.summary == {'distribution_count': 3, 'channel_count': 1}
    repeat = case.client.post('/api/uploads/preview', json={k: v for k, v in payload(setup_catalog).items() if k != 'idempotency_key'})
    assert repeat.status_code == 200 and repeat.json()['can_submit']
    assert not case.writes


def test_same_site_auth_partitions_keep_other_members_until_final_delete_then_allow_new_batch(db, login, container_site):
    client = login('user')
    category, fmt = setup_template(db, login('root'), container_site)
    body = request_body(category, fmt, AWS_KEYS)
    result = post(client, body)
    channel_id = result['items'][0]['channel_id']
    ids = [row['distribution_id'] for row in result['items']]
    assert len(ids) == 2
    original = db.get(Channel, channel_id).key_encrypted
    def remove(dist_id):
        return force(SimpleNamespace(client=client, channel=db.get(Channel, channel_id),
                                     dist=db.get(Distribution, dist_id)))
    first = remove(ids[0])
    assert first.status_code == 200, first.text
    assert not first.json()['channel_deleted']
    db.expire_all()
    assert db.get(Channel, channel_id).key_encrypted == original
    assert db.scalar(select(func.count()).select_from(ChannelCredential)) == 4
    assert db.get(Distribution, ids[1]) is not None
    assert remove(ids[1]).json()['channel_deleted']
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ChannelCredential)) == 0
    again = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'})
    assert again.status_code == 200 and again.json()['can_submit']
    second = post(client, {**body, 'idempotency_key': 'new-after-hard-delete'})
    assert second['items'][0]['channel_id'] != channel_id


@pytest.mark.parametrize('status', ['pending', 'failed', 'needs_review', 'expired', 'no_lease'])
def test_unresolved_or_expired_selected_steps_are_physically_removed(db, delete_case, status):
    case = delete_case
    item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == case.dist.id))
    item.status = 'running' if status in ('expired', 'no_lease') else status
    item.lease_until = utcnow() - timedelta(seconds=1) if status == 'expired' else None
    item_id = item.id
    db.commit()
    assert force(case).status_code == 200
    db.expire_all()
    assert db.get(TaskItem, item_id) is None


def test_running_selected_lease_is_busy_but_unselected_running_step_is_not(db, delete_case):
    case = delete_case
    item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == case.dist.id))
    item.status, item.lease_until = 'running', utcnow() + timedelta(seconds=120)
    db.commit()
    assert force(case).status_code == 409
    item.status = 'failed'
    other = db.scalar(select(TaskItem).where(TaskItem.distribution_id != case.dist.id))
    other.status, other.lease_until = 'running', utcnow() + timedelta(seconds=120)
    db.commit()
    assert force(case).status_code == 200
    db.refresh(other)
    assert other.status == 'running'


@pytest.mark.parametrize('locked_model', [Task, TaskItem, Distribution])
def test_purge_uses_nowait_for_work_and_remote_write_locks(db, delete_case, locked_model):
    item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == delete_case.dist.id))
    target_id = item.task_id if locked_model is Task else item.id if locked_model is TaskItem else delete_case.dist.id
    with SessionLocal() as session:
        session.scalar(select(locked_model).where(locked_model.id == target_id).with_for_update())
        assert force(delete_case).status_code == 409
        session.rollback()
    assert force(delete_case).status_code == 200


def test_order_reference_blocks_last_channel_deletion_without_changing_history(db, users, delete_case):
    case = delete_case
    fact = fact_for(db, users, case.channel, case.dist)
    order, line = order_for(db, users, case.channel, sources=[{'distribution_ids': [case.dist.id]}])
    db.commit()
    before = deepcopy(line.site_amounts)
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == case.channel.id)))
    response = force(case, ids=ids)
    assert response.status_code == 409 and '结算单' in response.text
    db.expire_all()
    assert db.get(UsageFact, fact.id) is not None and db.get(Distribution, case.dist.id) is not None
    assert db.get(SettlementOrderLine, line.id).site_amounts == before
    assert db.get(SettlementOrder, order.id).payment_amount == 8
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 3


def test_partial_deletion_preserves_frozen_order_sources(db, users, delete_case):
    case = delete_case
    removed_id = case.dist.id
    order, line = order_for(db, users, case.channel, sources=[{'distribution_ids': [removed_id], 'usage_amount': '8'}])
    db.commit()
    before = deepcopy(line.site_amounts)
    assert force(case).status_code == 200
    db.expire_all()
    assert db.get(Distribution, removed_id) is None
    assert db.get(SettlementOrderLine, line.id).site_amounts == before
    assert db.get(SettlementOrder, order.id).payment_amount == 8


def test_order_snapshot_lock_blocks_purge_until_transaction_ends(db, users, login, ledger):
    fact = ledger['facts'][2]
    channel = db.get(Channel, fact.channel_id)
    excluded = Distribution(channel_id=channel.id, site_id=fact.site_id, remote_id='987', remote_name='uncovered',
                            partition_key='excluded', remote_snapshot={'type': 1})
    db.add(excluded)
    db.commit()
    client = login('user')
    lock_billing_snapshot(db)
    result = client.post('/api/channels/' + channel.id + '/actions', json={
        'action': 'force_delete', 'distribution_ids': [excluded.id], 'confirmation': 'DELETE REMOTE AND LOCAL 1'})
    assert result.status_code == 409 and '正在处理' in result.text
    db.rollback()
    result = client.post('/api/channels/' + channel.id + '/actions', json={
        'action': 'force_delete', 'distribution_ids': [excluded.id], 'confirmation': 'DELETE REMOTE AND LOCAL 1'})
    assert result.status_code == 200


def test_shared_audit_keeps_survivor_object_and_removes_deleted_entity_contents(db, users, delete_case):
    case = delete_case
    other = db.scalar(select(Distribution).where(Distribution.channel_id == case.channel.id, Distribution.id != case.dist.id))
    survivor = {'distribution_id': other.id, 'remote_name': 'survivor-name', 'config': {'retain': True}}
    event = AuditEvent(actor_id=users['user'].id, actor_role='user', action='shared.fixture',
        object_type='channel', object_id=case.channel.id, summary={'targets': [
            {'distribution_id': case.dist.id, 'remote_name': 'must-be-removed', 'config': {'secret-detail': True}}, survivor]})
    db.add(event)
    db.commit()
    assert force(case).status_code == 200
    db.refresh(event)
    assert event.summary == {'targets': [survivor]}


def test_unclaimed_cleanup_is_exact_and_final_channel_clears_adopted_records(db, delete_case):
    case = delete_case
    selected = UnclaimedChannel(site_id=case.dist.site_id, remote_id=case.dist.remote_id,
                                remote_name='selected', adopted_channel_id=case.channel.id)
    other = UnclaimedChannel(site_id=case.dist.site_id, remote_id='other-remote', remote_name='unrelated')
    db.add_all([selected, other])
    db.commit()
    selected_id, other_id = selected.id, other.id
    assert force(case).status_code == 200
    db.expire_all()
    assert db.get(UnclaimedChannel, selected_id) is None
    assert db.get(UnclaimedChannel, other_id) is not None


def test_shared_task_and_cross_task_references_keep_unrelated_work(db, users, delete_case):
    case = delete_case
    original = db.scalar(select(TaskItem).where(TaskItem.distribution_id == case.dist.id))
    dead_task = Task(actor_id=users['user'].id, owner_id=users['user'].id,
        actor_session_version=users['user'].session_version, kind='test', status='failed', snapshot={})
    db.add(dead_task)
    db.flush()
    extra = TaskItem(task_id=dead_task.id, channel_id=case.channel.id, site_id=case.dist.site_id,
        distribution_id=case.dist.id, operation='test', snapshot={}, status='failed')
    db.add(extra)
    other = db.scalar(select(TaskItem).where(TaskItem.distribution_id != case.dist.id))
    other.snapshot = {**other.snapshot, 'expected': {'_monitoring': {'connectivity_test': {
        'task_id': dead_task.id, 'message': 'deleted-task-details'}}}, 'keep': 'survivor'}
    parent = db.get(Task, original.task_id)
    parent.snapshot = {**parent.snapshot, 'source_task_id': dead_task.id}
    db.commit()
    dead_id = dead_task.id
    assert force(case).status_code == 200
    db.expire_all()
    assert db.get(Task, dead_id) is None
    assert other.snapshot['keep'] == 'survivor' and 'connectivity_test' not in other.snapshot['expected']['_monitoring']
    assert 'source_task_id' not in parent.snapshot
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 2
