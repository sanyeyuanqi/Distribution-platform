"""Upload history and observed remote status are independent, scoped projections."""
# ruff: noqa: F811
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.channel_service import new_task
from app.db import utcnow
from app.distribution_status import observed_remote_state, upload_state
from app.models_channels import TaskItem
from sqlalchemy import delete, select
from test_channels import delete_case, setup_catalog  # noqa: F401


def dist(status='disabled', remote_id='91', raw=2, **overrides):
    return SimpleNamespace(status=status, remote_id=remote_id,
                           remote_snapshot={'id': 91, 'status': raw}, **overrides)


def item(status='succeeded', operation='create', kind='upload', error=None):
    return SimpleNamespace(status=status, operation=operation, task_kind=kind, error=error)


@pytest.mark.parametrize(('local', 'raw', 'expected', 'reason'), [
    ('enabled', 1, 'enabled', None), ('disabled', 2, 'disabled', 'manual'),
    ('unavailable', 3, 'disabled', 'automatic'), ('failed', 1, 'enabled', None),
    ('needs_review', 3, 'disabled', 'automatic'), ('created_pending_verification', 2, 'disabled', 'manual'),
    ('missing', 1, 'unavailable', None), ('deleted', 1, 'deleted', None),
    ('disabled', 0, 'unavailable', None), ('disabled', '3', 'unavailable', None),
    ('disabled', True, 'unavailable', None),
])
def test_remote_state_uses_snapshot_independently_of_upload_result(local, raw, expected, reason):
    assert observed_remote_state(dist(local, raw=raw)) == {
        'remote_status': expected, 'remote_disable_reason': reason}


@pytest.mark.parametrize('condition', ['no_id', 'mismatched_id', 'local_delete', 'empty_snapshot'])
def test_remote_state_needs_a_confirmed_matching_link(condition):
    row = dist()
    if condition == 'no_id':
        row.remote_id = None
    elif condition == 'mismatched_id':
        row.remote_snapshot['id'] = 92
    elif condition == 'local_delete':
        row.status = 'deleted'
        row.remote_snapshot['_local_deletion'] = {'remote_confirmed': False}
    else:
        row.remote_snapshot = {}
    assert observed_remote_state(row) == {'remote_status': 'unavailable', 'remote_disable_reason': None}


@pytest.mark.parametrize('operation', ['enable', 'disable', 'test', 'sync', 'sync_usage', 'edit', 'rotate', 'delete_remote'])
def test_later_operation_failure_never_overwrites_create_success(operation):
    result = upload_state(dist(), [item('failed', operation, error='later safe operation error'), item()])
    assert result == {'upload_status': 'succeeded', 'upload_message': '上传成功', 'upload_is_reupload': False}


@pytest.mark.parametrize(('status', 'expected'), [
    ('pending', 'pending'), ('running', 'running'), ('succeeded', 'succeeded'), ('failed', 'failed'),
    ('needs_review', 'needs_review'), ('cancelled', 'cancelled'), ('unknown', 'needs_review'),
    ('superseded', 'cancelled'), ('unexpected', 'unknown'),
])
def test_latest_create_or_reupload_wins_even_with_remote_snapshot(status, expected):
    result = upload_state(dist(), [item(status, kind='reupload'), item('failed', error='old create error')])
    assert result['upload_status'] == expected and result['upload_is_reupload'] is True


@pytest.mark.parametrize('status', ['pending', 'failed', 'needs_review', 'cancelled'])
def test_create_error_comes_from_create_step_only(status):
    result = upload_state(dist(), [item('failed', 'disable', error='wrong operation error'),
                                 item(status, error='目标站点拒绝了过长的渠道分组')])
    assert result['upload_message'] == '目标站点拒绝了过长的渠道分组'


@pytest.mark.parametrize('local', ['failed', 'pending', 'running', 'needs_review', 'created_pending_verification',
                                  'missing', 'deleted'])
def test_no_create_history_does_not_infer_success_from_unresolved_or_deleted_state(local):
    assert upload_state(dist(local), [])['upload_status'] == 'unknown'


def test_confirmed_legacy_channel_can_report_exists_without_inventing_task_history():
    row = dist('unavailable', raw=0)
    result = upload_state(row, [])
    assert result['upload_status'] == 'succeeded' and '历史记录' in result['upload_message']
    assert result['upload_is_reupload'] is False
    row.remote_snapshot['id'] = 92
    assert upload_state(row, [])['upload_status'] == 'unknown'


def get_row(case, client=None):
    response = (client or case.client).get(f'/api/channels/{case.channel.id}')
    assert response.status_code == 200, response.text
    return next(row for row in response.json()['distributions'] if row['id'] == case.dist.id)


def test_api_retains_original_upload_error_after_later_non_create_failure(db, users, login, delete_case):
    case = delete_case
    creation = db.scalar(select(TaskItem).where(TaskItem.distribution_id == case.dist.id,
                                                TaskItem.operation == 'create'))
    creation.status, creation.error = 'failed', '目标站点拒绝了过长的渠道分组'
    case.dist.status, case.dist.error = 'failed', 'later unrelated operation message'
    case.dist.remote_snapshot = {**case.remote, 'status': 3}
    later = new_task(db, users['user'], users['user'].id, 'disable')
    db.add(TaskItem(task_id=later.id, channel_id=case.channel.id, distribution_id=case.dist.id,
                    site_id=case.dist.site_id, operation='disable', status='failed',
                    error='later toggle error', snapshot={'key': 'snapshot-secret-must-not-leak'}))
    db.commit()
    for client in (case.client, login('admin'), login('root')):
        row = get_row(case, client)
        assert row['status'] == 'failed'
        assert row['remote_status'] == 'disabled' and row['remote_disable_reason'] == 'automatic'
        assert row['upload_status'] == 'failed' and row['upload_is_reupload'] is False
        assert row['upload_message'] == creation.error and row['error'] == case.dist.error
        assert 'snapshot-secret' not in str(row)
    assert login('other_user').get(f'/api/channels/{case.channel.id}').status_code == 404
    assert login('other_admin').get(f'/api/channels/{case.channel.id}').status_code == 404


def test_reupload_projection_tracks_newest_create_and_keeps_remote_state_independent(db, users, delete_case):
    case = delete_case
    original = db.scalar(select(TaskItem).where(TaskItem.distribution_id == case.dist.id,
                                               TaskItem.operation == 'create'))
    original.status, original.error = 'superseded', 'old failure'
    original.created_at = utcnow() - timedelta(days=1)
    case.dist.status = 'needs_review'
    case.dist.remote_snapshot = {**case.remote, 'status': 1}
    task = new_task(db, users['user'], users['user'].id, 'reupload')
    current = TaskItem(task_id=task.id, channel_id=case.channel.id, distribution_id=case.dist.id,
                       site_id=case.dist.site_id, operation='create', status='pending',
                       snapshot={'member_fingerprints': ['do-not-expose'], 'reupload': True})
    db.add(current)
    db.commit()
    snapshot_before = deepcopy(case.dist.remote_snapshot)
    for status in ('pending', 'running', 'failed', 'needs_review', 'succeeded', 'cancelled'):
        current.status = status
        current.error = 'current safe create error' if status in ('failed', 'needs_review') else None
        # Updating an older item must not make it the newest creation attempt.
        original.updated_at = utcnow() + timedelta(days=1)
        db.commit()
        row = get_row(case)
        assert row['upload_status'] == status and row['upload_is_reupload'] is True
        assert row['reupload_task']['id'] == task.id
        assert row['remote_status'] == 'enabled' and row['status'] == 'needs_review'
        assert 'do-not-expose' not in str(row)
        if current.error:
            assert row['upload_message'] == current.error
    db.refresh(case.dist)
    assert case.dist.remote_snapshot == snapshot_before and case.dist.status == 'needs_review'


def test_delete_changes_remote_status_without_rewriting_upload_history(db, delete_case):
    case = delete_case
    case.dist.status = 'deleted'
    db.commit()
    row = get_row(case)
    assert row['remote_status'] == 'deleted' and row['remote_disable_reason'] is None
    assert row['upload_status'] == 'succeeded'  # A historical create succeeded before deletion.
    db.execute(delete(TaskItem).where(TaskItem.distribution_id == case.dist.id))
    db.commit()
    assert get_row(case)['upload_status'] == 'unknown'  # Deletion alone does not prove upload success.


def test_legacy_api_fallback_requires_snapshot_but_does_not_create_history(db, delete_case):
    case = delete_case
    db.execute(delete(TaskItem).where(TaskItem.distribution_id == case.dist.id))
    case.dist.status = 'disabled'
    db.commit()
    row = get_row(case)
    assert row['upload_status'] == 'succeeded' and '历史记录' in row['upload_message']
    assert row['remote_status'] == 'disabled' and row['remote_disable_reason'] == 'manual'
    case.dist.remote_snapshot = {}
    db.commit()
    row = get_row(case)
    assert row['upload_status'] == 'unknown' and row['remote_status'] == 'unavailable'
    assert db.scalar(select(TaskItem.id).where(TaskItem.distribution_id == case.dist.id)) is None
