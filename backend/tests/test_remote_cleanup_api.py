"""Local-first deletion API contracts; no worker or real remote request runs here."""
# ruff: noqa: F811
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from app import remote_cleanup, worker
from app.channel_purge import references
from app.db import SessionLocal, utcnow
from app.models_billing import SettlementOrder, UsageFact
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from fastapi import HTTPException
from settlement_fixtures import order_for
from sqlalchemy import func, select
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_force_delete_local import fact_for
from test_force_delete_remote import force_remote  # noqa: F401


@pytest.fixture(autouse=True)
def api_only(force_remote, monkeypatch):
    def unexpected_remote(*args, **kwargs):
        pytest.fail('The local-first API must not contact a remote adapter')

    monkeypatch.setattr(worker, 'get_adapter', unexpected_remote)
    return force_remote


def request(client, channel_id, ids, *, nonce='local-first-delete-0001', **overrides):
    return client.post('/api/channels/' + channel_id + '/actions', json={
        'action': 'force_delete_async', 'distribution_ids': ids,
        'confirmation': f'DELETE LOCAL AND QUEUE REMOTE {len(ids)}',
        'idempotency_key': nonce, **overrides})


def cleanup_count(db):
    return db.scalar(select(func.count()).select_from(Task).where(Task.kind == 'force_delete_async'))


def test_post_purges_only_selected_partition_before_any_remote_request(db, users, delete_case, api_only):
    case = delete_case
    selected_id, channel_id = case.dist.id, case.channel.id
    survivor = db.scalar(select(Distribution).where(Distribution.channel_id == channel_id,
                                                    Distribution.id != selected_id))
    # A second partition on the same site must not be swallowed by a site-wide delete.
    survivor.site_id, survivor.partition_key = case.dist.site_id, 'keep-this-partition'
    survivor.template_snapshot = {'keep': ['unchanged']}
    for item in db.scalars(select(TaskItem).where(TaskItem.distribution_id == survivor.id)):
        item.site_id = survivor.site_id
    selected_fact = fact_for(db, users, case.channel, case.dist)
    survivor_fact = fact_for(db, users, case.channel, survivor)
    selected_version = DistributionVersion(distribution_id=selected_id, key_version=1)
    db.add(selected_version)
    db.commit()
    survivor_id, fact_id, surviving_fact_id, version_id = (
        survivor.id, selected_fact.id, survivor_fact.id, selected_version.id)
    before = {name: deepcopy(getattr(survivor, name)) for name in (
        'remote_id', 'remote_name', 'site_id', 'partition_key', 'status', 'models',
        'routing_group', 'key_version', 'template_snapshot', 'remote_snapshot')}
    ciphertext = case.channel.key_encrypted
    response = request(case.client, channel_id, [selected_id])
    assert response.status_code == 200, response.text
    assert response.json()['result'] == {
        'local_deleted': True, 'deleted_distribution_count': 1,
        'deleted_channel_count': 0, 'remote_confirmed': False}
    assert response.json()['kind'] == 'force_delete_async'
    assert response.json()['status'] == 'queued' and response.json()['total'] == 1
    db.expire_all()
    assert db.get(Distribution, selected_id) is None
    assert db.get(UsageFact, fact_id) is None and db.get(DistributionVersion, version_id) is None
    assert db.get(UsageFact, surviving_fact_id) is not None
    after = db.get(Distribution, survivor_id)
    assert {name: getattr(after, name) for name in before} == before
    assert db.get(Channel, channel_id).key_encrypted == ciphertext
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2
    assert not api_only.writes


def test_final_partition_purge_keeps_only_independent_outbox_and_replays_nonce(db, delete_case, api_only):
    case = delete_case
    channel_id, group_id = case.channel.id, case.channel.group_id
    rows = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel_id)))
    ids = [row.id for row in rows]
    old_item_ids = list(db.scalars(select(TaskItem.id)))
    old_task_ids = list(db.scalars(select(Task.id)))
    secret = 'fixture-secret-must-not-survive-local-purge'
    rows[0].remote_snapshot = {**rows[0].remote_snapshot, 'key': secret, 'nested': {'token': secret}}
    expected_targets = {row.site_id: {'id': row.remote_id, 'name': row.remote_name, 'type': 1} for row in rows}
    db.commit()

    response = request(case.client, channel_id, ids, nonce='cleanup-final-idempotency')
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert response.json()['result']['deleted_channel_count'] == 1
    assert response.json()['result']['remote_confirmed'] is False
    db.expire_all()
    for model in (Channel, ChannelCredential, KeyVersion, Distribution, UploadGroup):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    task = db.get(Task, task_id)
    assert task.group_id is None and task.request_hash and task.idempotency_key == 'cleanup-final-idempotency'
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)))
    assert len(items) == len(ids)
    removed = {channel_id, group_id, *ids, *old_item_ids, *old_task_ids}
    assert not references(task.snapshot, removed)
    for item in items:
        assert item.operation == 'remote_cleanup'
        assert item.channel_id is item.distribution_id is item.key_version is item.proxy_encrypted is None
        assert item.snapshot['delete_target'] == expected_targets[item.site_id]
        assert not item.remote_write_attempted
        assert not references(item.snapshot, removed)
        assert secret not in json.dumps(item.snapshot)
        assert 'placeholder-key-alpha' not in json.dumps(item.snapshot)
    assert all(db.get(TaskItem, item_id) is None for item_id in old_item_ids)
    assert secret not in response.text and 'snapshot' not in response.json()['items'][0]
    assert case.client.get('/api/channels/' + channel_id).status_code == 404
    detail = case.client.get('/api/channels/action-tasks/' + task_id)
    assert detail.status_code == 200 and detail.json()['id'] == task_id
    replay = request(case.client, channel_id, ids, nonce='cleanup-final-idempotency')
    assert replay.status_code == 200 and replay.json()['id'] == task_id
    assert cleanup_count(db) == 1 and not api_only.writes
    changed = request(case.client, channel_id, ids[:1], nonce='cleanup-final-idempotency')
    assert changed.status_code == 409


def test_foreign_owners_cannot_purge_or_queue_cleanup(db, login, delete_case):
    channel_id, dist_id = delete_case.channel.id, delete_case.dist.id
    for role in ('other_user', 'other_admin', 'sibling'):
        response = request(login(role), channel_id, [dist_id], nonce='foreign-delete-' + role)
        assert response.status_code == 404
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None
    assert cleanup_count(db) == 0


def test_order_reference_rejects_whole_batch_without_outbox(db, users, delete_case):
    case = delete_case
    channel_id = case.channel.id
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == channel_id)))
    fact = fact_for(db, users, case.channel, case.dist)
    order, _line = order_for(db, users, case.channel)
    db.commit()
    fact_id, order_id = fact.id, order.id
    response = request(case.client, channel_id, ids)
    assert response.status_code == 409 and '结算单' in response.text
    db.expire_all()
    assert all(db.get(Distribution, dist_id) is not None for dist_id in ids)
    assert db.get(Channel, channel_id) is not None and db.get(UsageFact, fact_id) is not None
    assert db.get(SettlementOrder, order_id).payment_amount == 8
    assert cleanup_count(db) == 0


def test_active_lease_rejects_local_purge_and_queue_atomically(db, delete_case):
    case = delete_case
    channel_id, dist_id = case.channel.id, case.dist.id
    item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist_id))
    item.status, item.stage = 'running', 'write_sent'
    item.remote_write_attempted = True
    item.lease_until = utcnow() + timedelta(minutes=5)
    db.commit()
    item_id = item.id
    response = request(case.client, channel_id, [dist_id])
    assert response.status_code == 409
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None
    assert db.get(TaskItem, item_id).status == 'running'
    assert cleanup_count(db) == 0


def test_scope_confirmation_and_nonce_are_required_before_local_changes(db, delete_case):
    case = delete_case
    channel_id, dist_id = case.channel.id, case.dist.id
    for changes in (
        {'confirmation': 'DELETE REMOTE AND LOCAL 1'}, {'confirmation': None},
        {'idempotency_key': None}, {'distribution_ids': [dist_id, dist_id]},
        {'site_ids': [case.dist.site_id]}, {'model': 'test-model'},
        {'distribution_ids': ['not-a-local-distribution']},
    ):
        response = request(case.client, channel_id, [dist_id], **changes)
        assert response.status_code == 422, response.text
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None and cleanup_count(db) == 0


def test_cleanup_list_detail_and_explicit_retry_follow_all_three_management_scopes(db, users, login, delete_case):
    case = delete_case
    response = request(case.client, case.channel.id, [case.dist.id])
    assert response.status_code == 200, response.text
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    for role in ('user', 'admin', 'root'):
        client = login(role)
        listing = client.get('/api/channels/action-tasks?kind=force_delete_async')
        assert listing.status_code == 200 and listing.json()['total'] == 1
        assert [row['id'] for row in listing.json()['items']] == [task_id]
        assert client.get('/api/channels/action-tasks/' + task_id).status_code == 200
        db.expire_all()
        task, item = db.get(Task, task_id), db.get(TaskItem, item_id)
        task.status, item.status, item.stage = 'needs_review', 'needs_review', 'write_sent'
        item.remote_write_attempted = True
        item.error = '本地已删除，远端结果待核实'
        item.snapshot = {**item.snapshot, 'retry_delete_authorized': False}
        db.commit()
        retried = client.post('/api/channels/action-tasks/' + task_id + '/retry')
        assert retried.status_code == 200, retried.text
        assert retried.json()['id'] == task_id and retried.json()['status'] == 'queued'
        db.expire_all()
        assert db.get(Task, task_id).execution_actor_id == users[role].id
        assert db.get(TaskItem, item_id).remote_write_attempted is True
        assert db.get(TaskItem, item_id).snapshot['retry_delete_authorized'] is True
        assert client.post('/api/channels/action-tasks/' + task_id + '/retry').status_code == 409
    for role in ('other_user', 'other_admin', 'sibling'):
        client = login(role)
        listing = client.get('/api/channels/action-tasks')
        assert listing.status_code == 200 and listing.json() == {'items': [], 'total': 0}
        assert client.get('/api/channels/action-tasks/' + task_id).status_code == 404
        assert client.post('/api/channels/action-tasks/' + task_id + '/retry').status_code == 404


def test_legacy_task_retry_and_reconcile_cannot_bypass_cleanup_contract(db, delete_case):
    case = delete_case
    response = request(case.client, case.channel.id, [case.dist.id])
    assert response.status_code == 200, response.text
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    db.expire_all()
    task, item = db.get(Task, task_id), db.get(TaskItem, item_id)
    task.status, item.status, item.stage = 'needs_review', 'needs_review', 'write_sent'
    item.remote_write_attempted = True
    db.commit()
    for suffix in ('retry', 'reconcile'):
        response = case.client.post(f'/api/tasks/{task_id}/{suffix}')
        assert response.status_code == 409
    db.expire_all()
    assert db.get(TaskItem, item_id).status == 'needs_review'
    assert db.get(TaskItem, item_id).stage == 'write_sent'
    assert db.get(TaskItem, item_id).remote_write_attempted is True
    assert db.get(TaskItem, item_id).snapshot.get('retry_delete_authorized') is not True


def test_unknown_create_without_remote_id_can_purge_but_is_not_declared_absent(db, delete_case):
    case = delete_case
    channel_id, dist_id, name = case.channel.id, case.dist.id, case.dist.remote_name
    case.dist.remote_id, case.dist.status = None, 'needs_review'
    item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist_id))
    item.status, item.stage, item.remote_write_attempted = 'needs_review', 'create_sent', True
    db.commit()
    response = request(case.client, channel_id, [dist_id])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Distribution, dist_id) is None
    cleanup = db.get(TaskItem, response.json()['items'][0]['id'])
    assert cleanup.snapshot['delete_target'] == {'id': None, 'name': name, 'type': 1}
    assert cleanup.snapshot['remote_never_created'] is False
    assert response.json()['result']['remote_confirmed'] is False


def test_busy_target_lock_does_not_leave_an_orphan_outbox(db, delete_case):
    case = delete_case
    channel_id, dist_id, site_id, remote_id = (
        case.channel.id, case.dist.id, case.dist.site_id, case.dist.remote_id)
    with SessionLocal() as other_session:
        remote_cleanup.lock_target(other_session, site_id, remote_id)
        response = request(case.client, channel_id, [dist_id])
        assert response.status_code == 409
        other_session.rollback()
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None
    assert cleanup_count(db) == 0


def test_purge_failure_rolls_back_already_flushed_outbox_and_local_deletions(db, delete_case, monkeypatch):
    case = delete_case
    channel_id, dist_id = case.channel.id, case.dist.id
    original = remote_cleanup.purge_distributions

    def fail_after_purge(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get('validate_only'):
            raise HTTPException(409, 'Injected conflict before atomic commit')
        return result

    monkeypatch.setattr(remote_cleanup, 'purge_distributions', fail_after_purge)
    response = request(case.client, channel_id, [dist_id])
    assert response.status_code == 409
    db.expire_all()
    assert db.get(Distribution, dist_id) is not None and db.get(Channel, channel_id) is not None
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 3
    assert cleanup_count(db) == 0


def test_cleanup_pagination_keeps_older_failure_and_owner_filtered_total(db, users, login, delete_case):
    case = delete_case
    channel_id = case.channel.id
    ids = list(db.scalars(select(Distribution.id).where(Distribution.channel_id == channel_id)))
    task_ids = []
    for index, dist_id in enumerate(ids):
        response = request(case.client, channel_id, [dist_id], nonce=f'cleanup-page-{index:04d}')
        assert response.status_code == 200, response.text
        task_ids.append(response.json()['id'])
    db.expire_all()
    now = utcnow()
    for task_id in task_ids:
        db.get(Task, task_id).created_at = now
    oldest = db.get(Task, task_ids[0])
    oldest.created_at, oldest.status = now - timedelta(days=1), 'failed'
    old_item = db.scalar(select(TaskItem).where(TaskItem.task_id == oldest.id))
    old_item.status, old_item.error = 'failed', '本地已删除，早期远端清理失败'
    foreign = Task(actor_id=users['other_user'].id, owner_id=users['other_user'].id,
                   actor_session_version=users['other_user'].session_version,
                   kind='force_delete_async', status='failed', created_at=now + timedelta(days=1),
                   snapshot={'local_result': {'local_deleted': True, 'deleted_distribution_count': 1,
                                              'deleted_channel_count': 1}})
    db.add(foreign)
    db.commit()
    foreign_id = foreign.id
    expected = [*sorted(task_ids[1:], reverse=True), task_ids[0]]
    for offset, task_id in enumerate(expected):
        page = case.client.get(f'/api/channels/action-tasks?offset={offset}&limit=1')
        assert page.status_code == 200 and page.json()['total'] == 3
        assert [row['id'] for row in page.json()['items']] == [task_id]
    second_page = case.client.get('/api/channels/action-tasks?offset=2&limit=2').json()
    assert second_page['total'] == 3
    assert [(row['id'], row['status']) for row in second_page['items']] == [(task_ids[0], 'failed')]
    assert second_page['items'][0]['can_retry'] is True
    assert case.client.get('/api/channels/action-tasks?offset=3&limit=1').json() == {'items': [], 'total': 3}
    clamped = case.client.get('/api/channels/action-tasks?offset=-1&limit=0').json()
    assert clamped['total'] == 3 and [row['id'] for row in clamped['items']] == expected[:1]
    other_page = login('other_user').get('/api/channels/action-tasks?offset=0&limit=1').json()
    assert other_page['total'] == 1 and [row['id'] for row in other_page['items']] == [foreign_id]
