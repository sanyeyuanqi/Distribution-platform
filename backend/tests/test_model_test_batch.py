"""One explicit test task durably reports each model without replaying sent inference."""
# ruff: noqa: F811
import json
from copy import deepcopy

import httpx
import pytest
from app import local_model_probe, worker
from app.adapters.silicon import RemoteError
from app.local_model_probe import probe_credential as real_probe_credential
from app.local_test_tasks import (
    initial_model_results,
    merge_provider_message,
    progress_result,
)
from app.models_channels import Task, TaskItem
from app.routers.channels import Action, action_request_values, force_request_hash
from app.security import fingerprint
from sqlalchemy import func, select
from test_channel_monitoring import observed, row  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_local_test_tasks import make_container


@pytest.fixture
def batch_case(db, observed):
    observed.dist.models = ['model-a', 'model-b', 'model-c']
    observed.dist.remote_snapshot = {**observed.dist.remote_snapshot, 'models': 'model-a,model-b,model-c'}
    db.commit()
    return observed


def queue(case, **overrides):
    return case.client.post(f'/api/channels/{case.channel.id}/actions', json={
        'action': 'test', 'distribution_ids': [case.dist.id], 'test_all': True,
        'idempotency_key': 'batch-model-test-0001', **overrides})


def state(case, client=None):
    return row(case, client)['connectivity_test']


@pytest.mark.parametrize('value', [None, '', ' \r\n\t ', 12, True, [], {}, 'x' * 1001, 'invalid\x00text'])
def test_invalid_content_never_creates_task_or_calls_provider(db, batch_case, value):
    before = db.scalar(select(func.count()).select_from(Task))
    response = queue(batch_case, test_content=value)
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert not batch_case.tests


@pytest.mark.parametrize('values', [
    {'model': 'model-a'}, {'test_all': 'true'}, {'test_all': 1},
    {'test_all': False}, {'test_all': False, 'model': 'outside'},
    {'action': 'sync_usage'}, {'action': 'disable', 'test_all': False, 'test_content': 'Hello'},
])
def test_invalid_mode_or_non_test_options_are_rejected(db, batch_case, values):
    before = db.scalar(select(func.count()).select_from(Task))
    assert queue(batch_case, **values).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == before


def test_all_rejects_empty_distribution_models(db, batch_case):
    batch_case.dist.models = []
    db.commit()
    assert queue(batch_case).status_code == 422


def test_all_models_are_frozen_progress_is_durable_and_admin_can_reopen(db, login, batch_case, monkeypatch):
    case, admin = batch_case, login('admin')
    payload = {'action': 'test', 'distribution_ids': [case.dist.id], 'test_all': True,
               'test_content': '  Explain a rainbow.  ', 'idempotency_key': 'admin-all-test-001'}
    response = admin.post(f'/api/channels/{case.channel.id}/actions', json=payload)
    assert response.status_code == 200, response.text
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    assert len(response.json()['items']) == 1 and response.json()['kind'] == 'test'
    initial = state(case, admin)
    assert initial['total_count'] == 3 and initial['completed_count'] == 0
    assert initial['current_model'] is None and initial['test_all'] is True
    assert initial['test_content'] == 'Explain a rainbow.'
    assert [entry['status'] for entry in initial['model_results']] == ['pending'] * 3
    assert 'success' not in initial
    assert admin.get('/api/tasks/' + task_id).status_code == 403
    assert admin.post(f'/api/channels/{case.channel.id}/actions', json=payload).json()['id'] == task_id
    assert queue(case, idempotency_key='second-inflight-test').status_code == 409
    calls, during = [], []

    def probe(key, schema, model, config, proxy='', before_request=None, *, content=None):
        before_request()
        calls.append((model, content))
        during.append(state(case, admin))
        # Durable in-flight state has no aggregate success, including after a prior model passed.
        with worker.SessionLocal() as read:
            stored = read.get(TaskItem, item_id).snapshot
            assert 'success' not in stored.get('operation_result', {})
            assert stored['test_progress']['current_model'] == model
        return {'success': True, 'latency_ms': 11, 'http_status': 200, 'message': 'private-provider-body'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: pytest.fail('No seller adapter'))
    assert worker.run_once(client=object())
    final = state(case, admin)
    assert calls == [(model, 'Explain a rainbow.') for model in ('model-a', 'model-b', 'model-c')]
    assert [entry['completed_count'] for entry in during] == [0, 1, 2]
    assert all(entry['status'] == 'running' and 'success' not in entry for entry in during)
    assert [entry['current_model'] for entry in during] == ['model-a', 'model-b', 'model-c']
    assert final['status'] == 'succeeded' and final['success'] is True and final['completed_count'] == 3
    assert final['current_model'] is None and final['tested_count'] == final['passed_count'] == 3
    assert final['failed_count'] == 0 and final['latency_ms'] == 33
    assert all(entry['status'] == 'succeeded' and entry['http_status'] == 200 for entry in final['model_results'])
    assert 'private-provider' not in json.dumps(final)
    assert admin.post(f'/api/channels/{case.channel.id}/actions', json=payload).json()['id'] == task_id
    assert len(calls) == 3


def test_each_key_is_tested_for_each_model_and_failure_http_wins(db, batch_case, monkeypatch):
    case = batch_case
    entries = make_container(db, case)
    response = queue(case)
    assert response.status_code == 200, response.text
    calls, during = [], []
    outcomes = [
        (False, 401, 'authentication_failed'), (True, 200, None),
        (True, 200, None), (True, 200, None),
        (False, 429, 'rate_limited'), (False, 503, 'provider_error'),
    ]

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        during.append(state(case))
        success, code, error = outcomes[len(calls)]
        calls.append((model, key))
        return {'success': success, 'http_status': code, 'error_code': error, 'latency_ms': 5,
                'message': 'DO NOT EXPOSE PROVIDER RESPONSE'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    assert calls == [(model, entry['key']) for model in case.dist.models for entry in entries]
    assert result['status'] == 'failed' and result['success'] is False
    assert result['completed_count'] == result['total_count'] == 3
    assert (result['tested_count'], result['passed_count'], result['failed_count']) == (6, 3, 3)
    rows = result['model_results']
    assert [entry['status'] for entry in rows] == ['failed', 'succeeded', 'failed']
    assert [entry['http_status'] for entry in rows] == [401, 200, 429]
    assert all(entry['tested_count'] == 2 and entry['latency_ms'] == 10 for entry in rows)
    assert during[1]['model_results'][0]['tested_count'] == 1  # Per-key checkpoint, not only per-model.
    assert 'DO NOT EXPOSE' not in str(result)
    assert case.client.post(f'/api/tasks/{response.json()["id"]}/retry').status_code == 409
    assert not worker.run_once(client=object()) and len(calls) == 6


@pytest.mark.parametrize('interruption', ['unknown', 'exception', 'cancel', 'guard'])
def test_partial_interruption_preserves_completed_rows_and_never_replays(db, batch_case, monkeypatch, interruption):
    case = batch_case
    response = queue(case)
    assert response.status_code == 200
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(model)
        if model == 'model-b':
            if interruption == 'unknown':
                raise RemoteError('测试结果未知', unknown=True)
            if interruption == 'exception':
                raise RuntimeError('raw-private-error')
            if interruption == 'cancel':
                db.get(Task, task_id).cancelled = True
                db.commit()
                before_request()
            raise worker.WriteStopped('当前上传凭据已改变，停止测试')
        return {'success': True, 'http_status': 200, 'latency_ms': 4}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    expected = 'needs_review' if interruption in ('unknown', 'exception') else 'cancelled'
    assert result['status'] == expected and 'success' not in result
    assert result['completed_count'] == 1 and result['total_count'] == 3 and result['current_model'] is None
    assert [entry['status'] for entry in result['model_results']] == ['succeeded', expected, 'not_tested']
    assert calls == ['model-a', 'model-b'] and 'raw-private-error' not in str(result)
    assert case.client.post(f'/api/tasks/{task_id}/retry').status_code == 409
    db.expire_all()
    assert 'success' not in db.get(TaskItem, item_id).snapshot.get('operation_result', {})
    if expected == 'needs_review':
        assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
        assert worker.run_once(client=object())
        assert state(case)['status'] == 'needs_review'
        assert state(case)['completed_count'] == 1
        assert calls == ['model-a', 'model-b']


def test_pending_cancel_has_no_completed_models(db, batch_case):
    response = queue(batch_case)
    assert response.status_code == 200
    assert batch_case.client.post(f'/api/tasks/{response.json()["id"]}/cancel').status_code == 200
    result = state(batch_case)
    assert result['status'] == 'cancelled' and result['completed_count'] == 0 and 'success' not in result
    assert [entry['status'] for entry in result['model_results']] == ['not_tested'] * 3
    assert not worker.run_once(client=object())


def test_single_mode_legacy_payload_and_frozen_full_scope_remain_compatible(db, batch_case):
    case = batch_case
    response = queue(case, test_all=False, model='model-b')
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    assert item.snapshot['test_models'] == ['model-b'] and item.snapshot['models'] == case.dist.models
    assert state(case)['total_count'] == 1 and not state(case)['test_all']
    assert worker.run_once(client=object())
    assert case.tests == ['model-b']
    assert state(case)['model'] == 'model-b' and state(case)['completed_count'] == 1


@pytest.mark.parametrize('finalized', [False, True])
def test_recovery_needs_final_operation_result_not_just_completed_progress(db, batch_case, monkeypatch, finalized):
    case = batch_case
    response = queue(case)
    assert response.status_code == 200
    task_id, item_id = response.json()['id'], response.json()['items'][0]['id']
    item = db.get(TaskItem, item_id)
    rows = initial_model_results(item.snapshot['test_models'])
    for entry in rows:
        entry.update(status='succeeded', tested_count=1, passed_count=1, http_status=200)
    progress = progress_result(rows)
    item.snapshot = {**item.snapshot, 'test_progress': progress,
                     **({'operation_result': {**progress, 'success': True}} if finalized else {})}
    item.status, item.stage, item.remote_write_attempted = 'needs_review', 'test_sent', True
    db.get(Task, task_id).status = 'needs_review'
    db.commit()
    monkeypatch.setattr(local_model_probe, 'probe_credential', lambda *args, **kwargs: pytest.fail('Never replay inference'))
    assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
    assert worker.run_once(client=object())
    result = state(case)
    assert result['completed_count'] == 3
    assert result['status'] == ('succeeded' if finalized else 'needs_review')
    assert (result.get('success') is True) is finalized


@pytest.mark.parametrize('operation', ['test', 'force_delete_async'])
def test_new_defaults_preserve_existing_idempotency_hashes(operation):
    values = {'action': operation, 'site_ids': None, 'distribution_ids': ['dist-id'],
              'confirmation': None, 'model': 'model-a' if operation == 'test' else None}
    body = Action(**values, idempotency_key='existing-request-id')
    assert action_request_values(body) == values
    assert force_request_hash('channel-id', body) == fingerprint(json.dumps(
        {'channel_id': 'channel-id', **values}, sort_keys=True, ensure_ascii=False))
    if operation == 'test':
        changed = deepcopy(values)
        changed['test_content'] = 'New request content'
        assert action_request_values(Action(**changed)) != values


@pytest.mark.parametrize('invalid', [None, 401, True, [], {}, '', ' \n\t ', 'bad\x00text', '\ud800'])
def test_provider_messages_only_accept_bounded_probe_text(invalid):
    messages = {'failed': [], 'succeeded': []}
    assert merge_provider_message(messages, {'success': False, 'provider_message': invalid,
        'message': 'raw key in unrelated outcome.message'}) is None


def test_provider_message_merge_is_deduplicated_failure_first_and_bounded():
    messages = {'failed': [], 'succeeded': []}
    assert merge_provider_message(messages, {'success': True, 'provider_message': 'OK'}) == 'OK'
    assert merge_provider_message(messages, {'success': True, 'provider_message': 'OK'}) == 'OK'
    assert merge_provider_message(messages, {'success': False, 'provider_message': 'Invalid API Key'}) == 'Invalid API Key\nOK'
    assert merge_provider_message(messages, {'success': False, 'provider_message': 'OK'}) == 'Invalid API Key\nOK'
    assert messages['succeeded'] == []  # Identical text moves to its failure position instead of duplicating.
    for index in range(1000):
        value = merge_provider_message(messages, {'success': False, 'provider_message': str(index) + 'x' * 5000})
    assert len(value) == 4000
    assert value.endswith('…')
    assert all(len(message) <= 1500 for bucket in messages.values() for message in bucket)
    assert sum(len(message) for bucket in messages.values() for message in bucket) < 5500
    assert value.startswith('Invalid API Key\nOK\n')


@pytest.mark.parametrize('second_message', ['Invalid API Key', 'Account access disabled'])
def test_provider_401_text_reaches_progress_final_api_and_readonly_recovery(db, batch_case, monkeypatch, second_message):
    case = batch_case
    make_container(db, case)
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200, response.text
    item_id, task_id = response.json()['items'][0]['id'], response.json()['id']
    assert state(case)['model_results'][0]['provider_message'] is None
    calls, during = [], []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        during.append(state(case))
        calls.append(model)
        return {'success': False, 'http_status': 401, 'error_code': 'authentication_failed',
                'provider_message': 'Invalid API Key' if len(calls) == 1 else second_message,
                'message': 'RAW-SECRET-KEY MUST NOT BE USED'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    expected = 'Invalid API Key' + ('\n' + second_message if second_message != 'Invalid API Key' else '')
    result = state(case)
    entry = result['model_results'][0]
    assert entry['provider_message'] == expected and entry['http_status'] == 401
    assert entry['message'].startswith('已测试 2 个密钥') and not entry['provider_message'].startswith('已测试')
    assert during[1]['model_results'][0]['provider_message'] == 'Invalid API Key'
    assert 'RAW-SECRET' not in str(result)
    # Simulate worker loss after its final result was persisted, before terminal status.
    db.expire_all()
    item = db.get(TaskItem, item_id)
    item.status, item.stage = 'needs_review', 'test_sent'
    db.get(Task, task_id).status = 'needs_review'
    db.commit()
    assert case.client.post(f'/api/tasks/{task_id}/reconcile').status_code == 200
    assert worker.run_once(client=object())
    assert state(case)['model_results'][0]['provider_message'] == expected
    assert len(calls) == 2


def test_provider_failure_message_precedes_success_without_altering_fallback(db, batch_case, monkeypatch):
    case = batch_case
    make_container(db, case)
    response = queue(case)
    assert response.status_code == 200
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(model)
        failed = len(calls) % 2 == 0
        return {'success': not failed, 'http_status': 401 if failed else 200,
                'error_code': 'authentication_failed' if failed else None,
                'provider_message': 'Invalid API Key' if failed else 'OK'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'failed' and result['completed_count'] == 3
    assert all(entry['provider_message'] == 'Invalid API Key\nOK' for entry in result['model_results'])
    assert all(entry['message'].startswith('已测试 2 个密钥') and entry['http_status'] == 401 for entry in result['model_results'])


def test_missing_provider_text_uses_safe_local_fallback_only(db, batch_case, monkeypatch):
    case = batch_case
    assert queue(case).status_code == 200

    def probe(key, schema, model, config, proxy='', before_request=None):
        return {'success': False, 'error_code': 'invalid_configuration', 'http_status': None,
                'message': 'credential=LOCAL-PRIVATE-KEY'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'failed' and result['completed_count'] == 3
    assert all(entry['provider_message'] is None and entry['message'] for entry in result['model_results'])
    assert all(entry['key_results'][0]['provider_message'] is None and entry['key_results'][0]['message']
               for entry in result['model_results'])
    assert 'LOCAL-PRIVATE' not in str(result)


@pytest.mark.parametrize('failure_first', [False, True])
def test_success_text_never_hides_another_keys_failure_without_provider_response(db, batch_case, monkeypatch, failure_first):
    case = batch_case
    make_container(db, case)
    assert queue(case, test_all=False, model='model-a').status_code == 200
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        failed = (len(calls) == 0) == failure_first
        calls.append(key)
        return {'success': not failed, 'http_status': None if failed else 200,
                'error_code': 'timeout' if failed else None, 'provider_message': None if failed else 'OK'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    entry = state(case)['model_results'][0]
    assert entry['status'] == 'failed' and entry['provider_message'] is None and entry['http_status'] is None
    assert '超时' in entry['message'] and (entry['passed_count'], entry['failed_count']) == (1, 1)


@pytest.mark.parametrize('first_failed', [False, True])
def test_unknown_key_keeps_prior_failure_response_but_not_unrelated_success_text(db, batch_case, monkeypatch, first_failed):
    case = batch_case
    make_container(db, case)
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(key)
        if len(calls) == 2:
            raise RemoteError('测试结果未知', unknown=True)
        return {'success': not first_failed, 'http_status': 401 if first_failed else 200,
                'error_code': 'authentication_failed' if first_failed else None,
                'provider_message': 'Invalid API Key' if first_failed else 'OK'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    expected = 'Invalid API Key' if first_failed else None
    result = state(case)
    assert result['status'] == 'needs_review' and result['completed_count'] == 0
    assert result['model_results'][0]['provider_message'] == expected
    assert case.client.post(f'/api/tasks/{response.json()["id"]}/reconcile').status_code == 200
    assert worker.run_once(client=object())
    assert state(case)['model_results'][0]['provider_message'] == expected and len(calls) == 2


def test_actual_probe_401_response_is_redacted_before_persistence_and_api(db, batch_case, monkeypatch):
    case, requests = batch_case, []
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200

    def send(method, url, **kwargs):
        assert method == 'POST' and url.startswith('https://api.openai.com/')
        requests.append((method, url))
        key = kwargs['headers']['Authorization'].removeprefix('Bearer ')
        return httpx.Response(401, json={'error': {'message': f'Invalid API Key {key}. Contact support.'}})

    monkeypatch.setattr(local_model_probe, 'probe_credential', real_probe_credential)
    monkeypatch.setattr(local_model_probe, 'safe_request', send)
    assert worker.run_once(client=object())
    result = state(case)
    entry = result['model_results'][0]
    assert entry['status'] == 'failed' and entry['http_status'] == 401
    assert entry['provider_message'] == 'Invalid API Key [REDACTED]. Contact support.'
    assert entry['key_results'][0]['provider_message'] == entry['provider_message']
    assert entry['key_results'][0]['http_status'] == 401 and entry['key_results'][0]['key_index'] == 1
    assert len(requests) == 1
    db.expire_all()
    persisted = db.get(TaskItem, response.json()['items'][0]['id']).snapshot['operation_result']
    assert persisted['model_results'][0]['provider_message'] == entry['provider_message']
