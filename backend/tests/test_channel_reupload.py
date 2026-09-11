"""Explicit one-partition reupload; isolated ledgers and fake remote channels only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest
from app import channel_reupload, worker
from app.adapters.silicon import RemoteError
from app.db import SessionLocal
from app.models import SiteUploadTemplate
from app.models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    Task,
    TaskItem,
)
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401
from test_multikey_containers import (
    AWS_KEYS,
    container_site,  # noqa: F401
    post,
    remotes,  # noqa: F401
    request_body,
    setup_template,
)
from test_worker_http import LocalLockClient


@pytest.fixture
def failed_case(db, login, container_site, remotes):  # noqa: F811
    site = container_site
    site.capabilities = {**site.capabilities, 'models': ['fixture-model', 'current-model'],
                         'groups': ['default', 'current-group']}
    db.commit()
    root, member = login('root'), login('user')
    category, fmt = setup_template(db, root, site)
    original = post(member, request_body(category, fmt, AWS_KEYS))
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == original['id'])))
    for item in items:
        item.status, item.stage, item.error, item.remote_write_attempted = 'failed', 'create_sent', 'old-safe-error', True
        dist = db.get(Distribution, item.distribution_id)
        dist.status, dist.error = 'failed', 'old-safe-error'
    task = db.get(Task, original['id'])
    task.status = 'failed'
    db.commit()
    item = next(item for item in items if db.get(Distribution, item.distribution_id).partition_label == 'Bedrock · AK/SK')
    dist = db.get(Distribution, item.distribution_id)
    return SimpleNamespace(site=site, fmt=fmt, category=category, root=root, member=member,
        channel=db.get(Channel, item.channel_id), dist=dist, item=item, task=task, items=items,
        template=db.scalar(select(SiteUploadTemplate).where(SiteUploadTemplate.site_id == site.id)), remotes=remotes)


def send(case, client=None, **overrides):
    return (client or case.member).post('/api/channels/' + case.channel.id + '/actions', json={
        'action': 'reupload', 'distribution_ids': [case.dist.id], 'idempotency_key': 'reupload-fixture-001', **overrides})


def projected(case, client=None):
    return next(dist for dist in (client or case.member).get('/api/channels/' + case.channel.id).json()['distributions']
                if dist['id'] == case.dist.id)


def test_current_keys_template_and_partition_used_without_overwriting_history(db, failed_case):
    case = failed_case
    old = (deepcopy(case.item.snapshot), case.item.error, case.item.remote_write_attempted, case.item.key_version)
    other = next(item for item in case.items if item.id != case.item.id)
    untouched = (deepcopy(other.snapshot), other.status, other.stage)
    new_keys = 'AKIA_NEW1|new-secret-one|us-east-1\nAKIA_NEW2|new-secret-two|us-east-1\nnew-api-one\nnew-api-two'
    rotated = case.member.post('/api/channels/' + case.channel.id + '/rotate', json={'key': new_keys})
    assert rotated.status_code == 200, rotated.text
    changed = case.root.patch('/api/upload-templates/' + case.template.id, json={
        'models': ['current-model'], 'routing_group': 'current-group', 'remark': 'Current template note'})
    assert changed.status_code == 200, changed.text
    assert projected(case)['reupload_available'] is True
    response = send(case)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task['kind'] == 'reupload' and task['owner_id'] == case.channel.owner_id and len(task['items']) == 1
    new = db.get(TaskItem, task['items'][0]['id'])
    assert new.distribution_id == case.dist.id and new.key_version == 2 and new.operation == 'create'
    assert new.snapshot['models'] == ['current-model'] and new.snapshot['routing_group'] == 'current-group'
    assert new.snapshot['key_count'] == 2 and new.snapshot['partition_key'] == case.dist.partition_key
    db.refresh(case.item)
    assert (case.item.status, case.item.stage) == ('cancelled', 'superseded')
    assert (case.item.snapshot, case.item.error, case.item.remote_write_attempted, case.item.key_version) == old
    db.refresh(other)
    assert (other.snapshot, other.status, other.stage) == untouched
    assert send(case).json()['id'] == task['id']  # Retry same HTTP request returns the same durable task.
    assert send(case, distribution_ids=[other.distribution_id]).status_code == 409
    assert send(case, idempotency_key='reupload-different-nonce').status_code == 409
    current = projected(case)
    assert current['reupload_task'] == {'id': task['id'], 'status': 'pending', 'error': None}
    assert current['reupload_available'] is False
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    applied = projected(case)
    assert applied['reupload_task']['status'] == 'succeeded' and applied['remote_id']
    assert applied['status'] == 'disabled' and applied['key_version'] == 2
    _, writes = case.remotes
    assert len(writes) == 1 and writes[0]['key'] == '\n'.join(new_keys.splitlines()[:2])
    assert writes[0]['models'] == 'current-model' and writes[0]['group'] == 'current-group'
    assert db.get(Distribution, case.dist.id).remote_name == case.dist.remote_name
    # Rotation also makes the untouched old partition's key stale. Preserve
    # the existing retry guard rather than replaying its previous credentials.
    retried = case.member.post('/api/tasks/' + case.task.id + '/retry')
    assert retried.status_code == 409 and '凭据版本已变化' in retried.json()['detail']
    db.refresh(case.item)
    assert (case.item.status, case.item.stage) == ('cancelled', 'superseded')
    assert db.get(TaskItem, other.id).status == 'failed'


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_owner_or_authorized_manager_can_reupload_and_poll_details(db, failed_case, login, role):
    case, client = failed_case, login(role)
    response = send(case, client)
    assert response.status_code == 200, response.text
    result = projected(case, client)
    assert result['reupload_task']['id'] == response.json()['id']
    assert result['reupload_task']['status'] == 'pending'
    if role == 'admin':
        assert client.get('/api/tasks/' + response.json()['id']).status_code == 403


def test_other_owner_cannot_read_or_reupload(db, failed_case, login):
    case, foreign = failed_case, login('other_user')
    assert send(case, foreign).status_code == 404
    assert foreign.get('/api/channels/' + case.channel.id).status_code == 404
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == 'reupload')) == 0


@pytest.mark.parametrize('change', ['remote', 'archived', 'deleted', 'pending', 'running', 'needs_review', 'no_failed_create'])
def test_ineligible_distribution_rejected_without_changing_original(db, failed_case, change):
    case = failed_case
    if change == 'remote': case.dist.remote_id, case.dist.status = 'confirmed-123', 'disabled'
    elif change == 'archived': case.channel.archived = True
    elif change == 'deleted': case.dist.status = 'deleted'
    elif change == 'no_failed_create': case.item.status = 'cancelled'
    else:
        db.add(TaskItem(task_id=case.task.id, channel_id=case.channel.id, site_id=case.site.id,
            distribution_id=case.dist.id, operation='test', status=change, snapshot={'test_source': 'local'}))
    db.commit()
    assert projected(case)['reupload_available'] is False
    assert send(case).status_code == 409
    db.refresh(case.item)
    assert case.item.stage == 'create_sent' and case.item.remote_write_attempted


@pytest.mark.parametrize('problem', ['disabled_template', 'disabled_site', 'unverified_site', 'wrong_service', 'models', 'group'])
def test_current_template_and_site_failures_have_safe_availability_and_no_task(db, failed_case, problem):
    case = failed_case
    if problem == 'disabled_template': case.template.enabled = False
    elif problem == 'disabled_site': case.site.enabled = False
    elif problem == 'unverified_site': case.site.verified_at = None
    elif problem == 'wrong_service': case.template.format_id = db.scalar(select(type(case.fmt).id).where(
        type(case.fmt).category_id == case.category.id, type(case.fmt).code == 'newapi-14-aws-claude-v1'))
    elif problem == 'models': case.template.models = []
    else: case.template.routing_group = 'private-unavailable-group'
    db.commit()
    result = projected(case)
    assert not result['reupload_available'] and result['reupload_reason']
    if problem == 'disabled_template': assert '模板已停用' in result['reupload_reason']
    assert 'private-unavailable-group' not in result['reupload_reason']
    response = send(case)
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == 'reupload')) == 0
    db.refresh(case.item)
    assert case.item.status == 'failed'


@pytest.mark.parametrize('override', [{'distribution_ids': []}, {'distribution_ids': ['unknown']},
    {'distribution_ids': ['one', 'two']}, {'site_ids': ['site']}, {'model': 'model'},
    {'confirmation': 'delete'}, {'idempotency_key': None}])
def test_request_requires_one_distribution_and_nonce(failed_case, override):
    assert send(failed_case, **override).status_code == 422


def test_existing_stable_name_is_never_claimed_with_current_key_even_through_reconcile(db, failed_case):
    case = failed_case
    records, writes = case.remotes
    records[(case.site.id, 'existing')] = {'id': 'existing', 'name': case.dist.remote_name}
    result = send(case).json()
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    new = db.get(TaskItem, result['items'][0]['id'])
    assert new.status == 'needs_review' and not new.remote_write_attempted
    assert case.member.post('/api/tasks/' + result['id'] + '/reconcile').status_code == 200
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    assert db.get(TaskItem, new.id).status == 'needs_review'
    assert db.get(Distribution, case.dist.id).remote_id is None
    assert db.scalar(select(func.count()).select_from(DistributionVersion)) == 0
    assert not writes


def test_acknowledged_create_can_finish_readback_later_without_second_post(db, failed_case, monkeypatch):
    case = failed_case
    factory = worker.get_adapter
    failures = [True]
    def adapter(site, before_write):
        remote = factory(site, before_write)
        detail = remote.detail
        def flaky(remote_id):
            if failures:
                failures.pop()
                raise RemoteError('读取暂时失败', category='connection_error')
            return detail(remote_id)
        remote.detail = flaky
        return remote
    monkeypatch.setattr(worker, 'get_adapter', adapter)
    result = send(case).json()
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    new = db.get(TaskItem, result['items'][0]['id'])
    assert new.status == 'needs_review' and new.snapshot['reupload_create_acknowledged'] is True
    assert case.member.post('/api/tasks/' + result['id'] + '/reconcile').status_code == 200
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    assert db.get(TaskItem, new.id).status == 'succeeded' and len(case.remotes[1]) == 1


def test_old_task_lock_conflict_fails_fast_without_superseding(db, failed_case):
    case = failed_case
    with SessionLocal() as other:
        other.scalar(select(Task).where(Task.id == case.task.id).with_for_update())
        response = send(case)
        assert response.status_code == 409 and '正在处理' in response.json()['detail']
    db.refresh(case.item)
    assert case.item.status == 'failed' and case.item.stage == 'create_sent'


def test_reupload_and_old_retry_cannot_reactivate_the_same_failed_snapshot(db, failed_case, monkeypatch):
    case = failed_case
    locked, release = Event(), Event()
    original = channel_reupload.choose_template
    def pause(*args):
        if not locked.is_set():
            locked.set()
            assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(channel_reupload, 'choose_template', pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        preparing = pool.submit(send, case)
        assert locked.wait(5)
        retrying = pool.submit(case.member.post, '/api/tasks/' + case.task.id + '/retry')
        release.set()
        prepared, retried = preparing.result(timeout=10), retrying.result(timeout=10)
    assert prepared.status_code == 200, prepared.text
    assert retried.status_code == 200, retried.text  # The untouched failed partition alone remains retryable.
    db.refresh(case.item)
    assert (case.item.status, case.item.stage) == ('cancelled', 'superseded')
    assert db.scalar(select(func.count()).select_from(TaskItem).where(TaskItem.distribution_id == case.dist.id,
        TaskItem.status == 'pending')) == 1
