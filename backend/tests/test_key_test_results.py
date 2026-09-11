"""Separate durable Key results preserve original indices and the no-replay fence."""
# ruff: noqa: F811
from copy import deepcopy

import pytest
from app import local_model_probe, worker
from app.adapters.silicon import RemoteError
from app.credential_containers import bundle_identity, encode_entries, partition_entries
from app.distribution_monitoring import test_progress_projection as project_progress
from app.local_test_tasks import initial_model_results, progress_result
from app.models import CredentialFormat
from app.models_channels import KeyVersion, Task, TaskItem
from app.security import encrypt
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from test_channel_monitoring import observed  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_model_test_batch import batch_case, queue, state  # noqa: F401


def four_keys(db, case, *, split=False):
    channel = case.channel
    fmt = db.get(CredentialFormat, channel.format_id)
    entries = [{'key': f'sk-private-key-{index}', 'remark': str(index % 2) if split else '', 'proxy': ''}
               for index in range(4)]
    channel.key_mode, channel.key_count = 'multiple', 4
    channel.key_encrypted = encrypt(encode_entries(entries))
    channel.fingerprint = bundle_identity(entries, fmt)
    version = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id))
    version.key_encrypted, version.fingerprint = channel.key_encrypted, channel.fingerprint
    part = partition_entries(entries, fmt)[-1]
    case.dist.partition_key, case.dist.key_count = part['partition_key'], part['key_count']
    db.commit()
    return part


def keys(result):
    return [key for model in result['model_results'] for key in model['key_results']]


@pytest.mark.parametrize('test_all', [False, True])
def test_each_key_has_its_own_message_http_latency_and_durable_progress(db, batch_case, monkeypatch, test_all):
    case = batch_case
    four_keys(db, case)
    response = queue(case, **({} if test_all else {'test_all': False, 'model': 'model-a'}))
    assert response.status_code == 200, response.text
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    models = case.dist.models if test_all else ['model-a']
    initial = state(case)
    assert db.get(TaskItem, item_id).snapshot['test_key_indices'] == [1, 2, 3, 4]
    assert initial['total_key_count'] == len(models) * 4
    assert initial['completed_key_count'] == initial['passed_key_count'] == initial['failed_key_count'] == 0
    assert initial['current_key_index'] is None
    assert [key['key_index'] for key in keys(initial)] == [1, 2, 3, 4] * len(models)
    assert all(key['status'] == 'pending' and key['provider_message'] is None for key in keys(initial))
    outcomes = [(True, 200, None, 'First response'), (False, 401, 'authentication_failed', 'Account disabled'),
                (False, 429, 'rate_limited', 'Quota exhausted'), (True, 200, None, 'Fourth response')]
    calls, during = [], []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        # OAuth and inference are still one Key result, even with two callbacks.
        before_request()
        during.append(state(case))
        index = len(calls) % 4
        calls.append((model, key))
        success, code, error, text = outcomes[index]
        return {'success': success, 'http_status': code, 'error_code': error, 'provider_message': text,
                'latency_ms': (index + 1) * 11, 'message': 'PRIVATE-UNTRUSTED-MESSAGE'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'failed' and result['success'] is False
    assert result['completed_count'] == result['total_count'] == len(models)
    assert result['completed_key_count'] == result['total_key_count'] == len(models) * 4
    assert result['passed_key_count'] == result['failed_key_count'] == len(models) * 2
    assert result['current_key_index'] is None
    assert [entry['completed_key_count'] for entry in during] == list(range(len(calls)))
    assert [entry['current_key_index'] for entry in during] == [1, 2, 3, 4] * len(models)
    for index, entry in enumerate(during):
        assert [key['status'] for key in keys(entry)][index] == 'running'
        assert 'success' not in entry
    for model in result['model_results']:
        assert [key['status'] for key in model['key_results']] == ['succeeded', 'failed', 'failed', 'succeeded']
        assert [key['provider_message'] for key in model['key_results']] == [entry[3] for entry in outcomes]
        assert [key['http_status'] for key in model['key_results']] == [200, 401, 429, 200]
        assert [key['latency_ms'] for key in model['key_results']] == [11, 22, 33, 44]
        assert all(not key['message'].startswith('已测试') for key in model['key_results'])
    assert 'sk-private-key' not in str(result) and 'PRIVATE-UNTRUSTED' not in str(result)
    # Durable final results survive read-only reconciliation with no second probe.
    db.expire_all()
    item = db.get(TaskItem, item_id)
    item.status, item.stage = 'needs_review', 'test_sent'
    db.get(Task, task_id).status = 'needs_review'
    db.commit()
    assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
    assert worker.run_once(client=object())
    assert state(case)['model_results'] == result['model_results']
    assert len(calls) == len(models) * 4


def test_partition_indices_are_original_container_positions_not_renumbered(db, batch_case, monkeypatch):
    case = batch_case
    part = four_keys(db, case, split=True)
    assert part['indices'] == [1, 3]
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200, response.text
    assert db.get(TaskItem, response.json()['items'][0]['id']).snapshot['test_key_indices'] == [2, 4]
    assert [key['key_index'] for key in keys(state(case))] == [2, 4]
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append((key, state(case)['current_key_index']))
        return {'success': True, 'provider_message': 'OK', 'http_status': 200}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    assert calls == [(entry['key'], index + 1) for entry, index in zip(part['entries'], part['indices'], strict=True)]
    assert state(case)['completed_key_count'] == 2


@pytest.mark.parametrize('interruption', ['unknown', 'exception', 'cancel', 'guard'])
def test_second_key_interruption_keeps_completed_result_and_does_not_replay(db, batch_case, monkeypatch, interruption):
    case = batch_case
    four_keys(db, case)
    response = queue(case)
    assert response.status_code == 200
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(key)
        if len(calls) == 2:
            if interruption == 'unknown':
                raise RemoteError('结果未知', unknown=True)
            if interruption == 'exception':
                raise RuntimeError('DO-NOT-PUBLISH')
            if interruption == 'cancel':
                db.get(Task, task_id).cancelled = True
                db.commit()
                before_request()
            raise worker.WriteStopped('凭据版本已改变')
        return {'success': True, 'http_status': 200, 'latency_ms': 7, 'provider_message': 'Key one response'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    expected = 'needs_review' if interruption in ('unknown', 'exception') else 'cancelled'
    result = state(case)
    assert result['status'] == expected and 'success' not in result
    assert result['completed_count'] == 0 and result['completed_key_count'] == result['passed_key_count'] == 1
    assert result['total_key_count'] == 12 and result['failed_key_count'] == 0
    assert result['current_model'] is None and result['current_key_index'] is None
    projected = keys(result)
    assert [key['status'] for key in projected] == ['succeeded', expected] + ['not_tested'] * 10
    assert projected[0]['provider_message'] == 'Key one response' and projected[0]['http_status'] == 200
    assert projected[0]['latency_ms'] == 7
    assert all(key['provider_message'] is None and key['http_status'] is None for key in projected[1:])
    db.expire_all()
    stored = db.get(TaskItem, item_id).snapshot
    assert 'success' not in stored.get('operation_result', {})
    assert [key['status'] for key in keys(stored['test_progress'])] == ['succeeded', 'running'] + ['pending'] * 10
    assert 'DO-NOT-PUBLISH' not in str(result)
    if expected == 'needs_review':
        assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
        assert worker.run_once(client=object())
        assert state(case)['model_results'] == result['model_results']
    assert len(calls) == 2


def test_cancel_before_execution_marks_all_key_rows_not_tested(db, batch_case):
    four_keys(db, batch_case)
    response = queue(batch_case)
    assert response.status_code == 200
    assert batch_case.client.post(f'/api/tasks/{response.json()["id"]}/cancel').status_code == 200
    result = state(batch_case)
    assert result['status'] == 'cancelled' and result['total_key_count'] == 12
    assert result['completed_key_count'] == result['failed_key_count'] == result['passed_key_count'] == 0
    assert all(key['status'] == 'not_tested' for key in keys(result))
    assert not worker.run_once(client=object())


@pytest.mark.parametrize('indices', [[2, 1, 3, 4], [1, 2], [True, 2, 3, 4]])
def test_changed_frozen_key_indices_stop_before_probe(db, batch_case, monkeypatch, indices):
    four_keys(db, batch_case)
    response = queue(batch_case)
    assert response.status_code == 200
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    item.snapshot = {**item.snapshot, 'test_key_indices': indices}
    # Python equality treats True == 1; explicitly persist this corrupted JSON fixture.
    flag_modified(item, 'snapshot')
    db.commit()
    monkeypatch.setattr(local_model_probe, 'probe_credential', lambda *a, **kw: pytest.fail('Do not probe changed Key plan'))
    assert worker.run_once(client=object())
    assert state(batch_case)['status'] == 'cancelled'
    assert state(batch_case)['completed_key_count'] == 0


def test_per_key_provider_text_is_bounded_and_missing_values_keep_local_fallback(db, batch_case, monkeypatch):
    four_keys(db, batch_case)
    assert queue(batch_case, test_all=False, model='model-a').status_code == 200
    messages = ['x' * 1600, {'message': 'DO-NOT-PUBLISH'}, None, 'A separate response']
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        message = messages[len(calls)]
        calls.append(key)
        return {'success': False, 'error_code': 'timeout', 'provider_message': message,
                'message': 'PRIVATE LOCAL ERROR', 'http_status': None}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(batch_case)
    values = keys(result)
    assert [key['provider_message'] for key in values] == ['x' * 1499 + '…', None, None, 'A separate response']
    assert all(key['status'] == 'failed' and '超时' in key['message'] and key['http_status'] is None for key in values)
    assert result['failed_key_count'] == result['completed_key_count'] == 4
    assert 'DO-NOT-PUBLISH' not in str(result) and 'PRIVATE LOCAL' not in str(result)


@pytest.mark.parametrize('sent', [False, True])
def test_legacy_missing_key_details_are_only_initialized_for_unsent_task(db, batch_case, monkeypatch, sent):
    case = batch_case
    four_keys(db, case, split=True)
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    item = db.get(TaskItem, item_id)
    snapshot = deepcopy(item.snapshot)
    snapshot.pop('test_key_indices')
    snapshot['test_progress'] = progress_result(initial_model_results(['model-a']))
    for row in snapshot['test_progress']['model_results']:
        row.pop('key_results')
    if sent:
        snapshot['test_progress']['model_results'][0].update(status='running', passed_count=1, tested_count=1)
        item.status, item.stage, item.remote_write_attempted = 'needs_review', 'test_sent', True
        db.get(Task, task_id).status = 'needs_review'
    item.snapshot = snapshot
    db.commit()
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        assert not sent, 'Never replay a sent legacy task'
        before_request()
        calls.append(key)
        return {'success': True, 'provider_message': 'OK'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    if sent:
        assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
    assert worker.run_once(client=object())
    result = state(case)
    if sent:
        assert result['status'] == 'needs_review'
        assert result['model_results'][0]['key_results'] == []
        assert result['total_key_count'] == result['completed_key_count'] == 0 and calls == []
    else:
        assert result['status'] == 'succeeded' and result['completed_key_count'] == 2
        assert [key['key_index'] for key in keys(result)] == [2, 4]
        db.expire_all()
        assert db.get(TaskItem, item_id).snapshot['test_key_indices'] == [2, 4]


@pytest.mark.parametrize('status', ['failed', 'needs_review', 'cancelled', 'succeeded'])
def test_projection_copies_nested_key_rows_and_does_not_count_interruption_as_completion(status):
    rows = initial_model_results(['model-a'], [2, 4, 6])
    rows[0]['status'] = 'running'
    rows[0]['key_results'][0].update(status='succeeded', http_status=200, provider_message='Finished')
    rows[0]['key_results'][1]['status'] = 'running'
    snapshot = {'test_progress': progress_result(rows, 'model-a', 4)}
    before = deepcopy(snapshot)
    rows[0]['key_results'][0]['provider_message'] = 'Changed after checkpoint'
    projected = project_progress(snapshot, status, {})
    assert snapshot == before
    expected = 'needs_review' if status == 'succeeded' else status
    assert [key['status'] for key in keys(projected)] == ['succeeded', expected, 'not_tested']
    assert keys(projected)[0]['provider_message'] == 'Finished'
    assert projected['completed_key_count'] == 1 and projected['failed_key_count'] == 0
    assert projected['current_key_index'] is None
