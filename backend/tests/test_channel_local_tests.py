"""Channel tests use confirmed uploads, one original Key/model call and durable results."""
# ruff: noqa: F811
from copy import deepcopy

import pytest
from app import local_model_probe, worker
from app.adapters.silicon import RemoteError
from app.credential_containers import channel_entries, partition_entries
from app.models import CredentialFormat, Site
from app.models_channels import Distribution, DistributionVersion, Task, TaskItem
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from test_channel_monitoring import observed  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_key_test_results import four_keys


def confirm(db, case, dist, models, *, part=None, config=None):
    dist.status, dist.models = 'disabled', list(models)
    dist.remote_snapshot = {**dist.remote_snapshot, 'models': ','.join(models), 'status': 2}
    if part:
        dist.partition_key, dist.key_count = part['partition_key'], part['key_count']
    if config is not None:
        dist.template_snapshot = {**dist.template_snapshot, 'effective_channel_config': config}
    version = db.scalar(select(DistributionVersion).where(
        DistributionVersion.distribution_id == dist.id, DistributionVersion.valid_to.is_(None)))
    if not version:
        db.add(DistributionVersion(distribution_id=dist.id, key_version=case.channel.key_version))
    db.commit()


@pytest.fixture
def channel_case(db, observed):
    case = observed
    others = list(db.scalars(select(Distribution).where(
        Distribution.channel_id == case.channel.id, Distribution.id != case.dist.id).order_by(Distribution.id)))
    case.sources = [case.dist, *others]
    confirm(db, case, case.dist, ['model-a'])
    return case


def queue(case, client=None, **overrides):
    return (client or case.client).post(f'/api/channels/{case.channel.id}/actions', json={
        'action': 'test', 'test_scope': 'channel', 'test_all': True,
        'idempotency_key': 'channel-model-test-0001', **overrides})


def channel_json(case, client=None):
    response = (client or case.client).get('/api/channels/' + case.channel.id)
    assert response.status_code == 200, response.text
    return response.json()


def state(case, client=None):
    return channel_json(case, client)['connectivity_test']


def key_rows(result):
    return [key for model in result['model_results'] for key in model['key_results']]


def install_probe(monkeypatch, calls):
    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append((key, model, deepcopy(config)))
        return {'success': True, 'http_status': 200, 'latency_ms': 7, 'provider_message': 'Provider OK'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    monkeypatch.setattr(worker, 'get_adapter', lambda *a, **kw: pytest.fail('Never test through a seller endpoint'))


def test_model_union_uses_confirmed_remote_models_not_unsent_target_or_channel_catalog(db, channel_case):
    case = channel_case
    confirm(db, case, case.sources[1], ['model-b', 'model-a'])
    case.dist.models = ['unsent-model']
    case.channel.models = ['channel-catalog-only']
    case.sources[2].models = ['not-uploaded']
    case.sources[2].remote_snapshot = {**case.sources[2].remote_snapshot, 'models': 'not-uploaded'}
    db.commit()
    public = channel_json(case)
    assert public['local_test_models'] == ['model-a', 'model-b']
    assert public['local_test_available'] is True and public['local_test_reason'] is None
    before = db.scalar(select(func.count()).select_from(Task))
    response = queue(case, test_all=False, model='unsent-model')
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Task)) == before
    response = queue(case, test_all=False, model='model-a')
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    assert item.snapshot['test_models'] == ['model-a']
    assert {source['distribution_id'] for source in item.snapshot['test_sources']} == {
        case.sources[0].id, case.sources[1].id}


@pytest.mark.parametrize('missing_proof', ['snapshot_models', 'current_version', 'remote_link'])
def test_missing_uploaded_proof_has_no_catalog_fallback_and_creates_no_task(db, channel_case, missing_proof):
    case = channel_case
    if missing_proof == 'snapshot_models':
        case.dist.remote_snapshot = {key: value for key, value in case.dist.remote_snapshot.items() if key != 'models'}
    elif missing_proof == 'current_version':
        version = db.scalar(select(DistributionVersion).where(DistributionVersion.distribution_id == case.dist.id))
        version.key_version = 0
    else:
        case.dist.remote_id = None
    db.commit()
    public = channel_json(case)
    assert public['local_test_models'] == [] and public['local_test_available'] is False
    assert public['local_test_reason']
    before = db.scalar(select(func.count()).select_from(Task))
    response = queue(case)
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert case.tests == []


@pytest.mark.parametrize('scope', [{'site_ids': []}, {'distribution_ids': []}])
def test_channel_scope_rejects_per_distribution_selectors(db, channel_case, scope):
    before = db.scalar(select(func.count()).select_from(Task))
    assert queue(channel_case, **scope).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == before


def test_partition_model_union_preserves_each_models_original_key_indices(db, channel_case, monkeypatch):
    case = channel_case
    four_keys(db, case, split=True)
    fmt = db.get(CredentialFormat, case.channel.format_id)
    parts = partition_entries(channel_entries(case.channel), fmt)
    confirm(db, case, case.sources[0], ['model-a', 'shared-model'], part=parts[0])
    confirm(db, case, case.sources[1], ['model-b', 'shared-model'], part=parts[1])
    response = queue(case)
    assert response.status_code == 200, response.text
    initial = state(case)
    assert {row['model']: [key['key_index'] for key in row['key_results']] for row in initial['model_results']} == {
        'model-a': [1, 3], 'model-b': [2, 4], 'shared-model': [1, 2, 3, 4]}
    calls = []
    install_probe(monkeypatch, calls)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'succeeded' and result['completed_key_count'] == result['total_key_count'] == 8
    assert {(key, model) for key, model, _ in calls} == {
        *((f'sk-private-key-{index}', 'model-a') for index in [0, 2]),
        *((f'sk-private-key-{index}', 'model-b') for index in [1, 3]),
        *((f'sk-private-key-{index}', 'shared-model') for index in range(4)),
    }
    assert len(calls) == 8


def test_same_uploaded_keys_across_sites_are_tested_once_for_each_model(db, channel_case, monkeypatch):
    case = channel_case
    part = four_keys(db, case)
    for dist in case.sources:
        confirm(db, case, dist, ['model-b', 'model-a'], part=part)
    response = queue(case)
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 1 and response.json()['kind'] == 'test'
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    assert item.distribution_id is None and item.channel_id == case.channel.id
    original = {dist.id: deepcopy(dist.remote_snapshot) for dist in case.sources}
    calls = []
    install_probe(monkeypatch, calls)
    assert worker.run_once(client=object())
    assert len(calls) == len({(key, model) for key, model, _ in calls}) == 8
    assert state(case)['completed_key_count'] == 8
    db.expire_all()
    assert {dist.id: dist.remote_snapshot for dist in case.sources} == original


def test_conflicting_mapping_fails_only_affected_key_rows_without_request(db, channel_case, monkeypatch):
    case = channel_case
    four_keys(db, case, split=True)
    parts = partition_entries(channel_entries(case.channel), db.get(CredentialFormat, case.channel.format_id))
    confirm(db, case, case.sources[0], ['model-a'], part=parts[0], config={'model_mapping': {'model-a': 'target-one'}})
    confirm(db, case, case.sources[1], ['model-a'], part=parts[0], config={'model_mapping': {'model-a': 'target-two'}})
    confirm(db, case, case.sources[2], ['model-a'], part=parts[1], config={'model_mapping': {'model-a': 'target-one'}})
    assert queue(case).status_code == 200
    calls = []
    install_probe(monkeypatch, calls)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'failed' and result['completed_key_count'] == 4
    assert result['passed_key_count'] == result['failed_key_count'] == 2
    assert [row['status'] for row in key_rows(result)] == ['failed', 'succeeded', 'failed', 'succeeded']
    for row in key_rows(result)[::2]:
        assert row['http_status'] is None and row['provider_message'] is None
        assert '不一致' in row['message']
    assert [(key, model) for key, model, _ in calls] == [('sk-private-key-1', 'model-a'), ('sk-private-key-3', 'model-a')]


def test_unused_rpm_routing_and_other_model_mapping_do_not_create_conflicts(db, channel_case, monkeypatch):
    case = channel_case
    confirm(db, case, case.sources[0], ['model-a'], config={
        'base_url': 'https://api.openai.com/v1/', 'model_mapping': {'model-a': 'same-target', 'unselected': 'one'},
        'rpm': 100, 'group': 'one', 'status': 1})
    confirm(db, case, case.sources[1], ['model-a'], config={
        'base_url': 'https://api.openai.com', 'model_mapping': {'model-a': 'same-target', 'unselected': 'two'},
        'rpm': 900, 'group': 'two', 'status': 2})
    # Paused sellers and disabled remote channels do not prohibit an already-uploaded local test.
    db.get(Site, case.sources[0].site_id).enabled = False
    db.commit()
    assert queue(case).status_code == 200
    calls = []
    install_probe(monkeypatch, calls)
    assert worker.run_once(client=object())
    assert state(case)['status'] == 'succeeded' and len(calls) == 1
    assert calls[0][2]['model_mapping']['model-a'] == 'same-target'


def test_admin_can_reopen_channel_progress_with_idempotency_and_no_private_source_plan(db, login, channel_case, monkeypatch):
    case, admin = channel_case, login('admin')
    for who in ('other_admin', 'other_user'):
        assert queue(case, client=login(who)).status_code == 404
    response = queue(case, client=admin)
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert admin.get('/api/tasks/' + task_id).status_code == 403
    assert state(case, admin)['status'] == 'pending'
    assert queue(case, client=admin).json()['id'] == task_id
    assert queue(case, client=admin, idempotency_key='another-channel-test').status_code == 409
    during = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        during.append(channel_json(case, admin))
        return {'success': True, 'http_status': 200, 'provider_message': 'Official response'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    assert during[0]['connectivity_test']['status'] == 'running'
    assert during[0]['connectivity_test']['current_key_index'] == 1
    final = channel_json(case, admin)
    assert final['connectivity_test']['status'] == 'succeeded'
    assert key_rows(final['connectivity_test'])[0]['provider_message'] == 'Official response'
    assert queue(case, client=admin).json()['id'] == task_id
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == 'test')) == 1
    for public in [*during, final]:
        text = str(public)
        assert all(secret not in text for secret in ('test_sources', 'test_plan', 'test_inputs', 'key_encrypted', 'placeholder-key-alpha'))
        assert all(dist['connectivity_test']['task_id'] != task_id for dist in public['distributions'])


def test_changed_source_model_stops_at_before_request_without_using_other_sources(db, channel_case, monkeypatch):
    case = channel_case
    confirm(db, case, case.sources[1], ['model-a'])
    assert queue(case).status_code == 200
    sent = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        case.sources[1].remote_snapshot = {**case.sources[1].remote_snapshot, 'models': 'different-model'}
        db.commit()
        before_request()
        sent.append(model)
        pytest.fail('Do not send after any frozen source loses its confirmed model')

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    result = state(case)
    assert result['status'] == 'cancelled' and result['completed_key_count'] == 0
    assert result['current_key_index'] is None and sent == []


def test_every_frozen_source_stays_locked_during_request_including_non_anchor(db, channel_case, monkeypatch):
    case = channel_case
    confirm(db, case, case.sources[1], ['model-a'])
    response = queue(case)
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    source_ids = {source['distribution_id'] for source in item.snapshot['test_sources']}
    assert len(source_ids) == 2 and item.distribution_id is None
    assert any(dist.site_id != item.site_id for dist in case.sources if dist.id in source_ids)
    checked = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        for source_id in sorted(source_ids):
            with worker.SessionLocal() as competing, pytest.raises(OperationalError):
                competing.scalar(select(Distribution.id).where(Distribution.id == source_id)
                                 .with_for_update(nowait=True))
            checked.append(source_id)
        return {'success': True, 'http_status': 200}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    assert state(case)['status'] == 'succeeded' and set(checked) == source_ids


def test_unknown_second_key_preserves_first_and_readonly_reconciliation_never_replays(db, channel_case, monkeypatch):
    case = channel_case
    four_keys(db, case)
    response = queue(case)
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(key)
        if len(calls) == 2:
            raise RemoteError('结果未知', unknown=True)
        return {'success': True, 'http_status': 200, 'provider_message': 'First key finished'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    initial = state(case)
    assert initial['status'] == 'needs_review' and initial['completed_key_count'] == 1
    assert [row['status'] for row in key_rows(initial)] == ['succeeded', 'needs_review', 'not_tested', 'not_tested']
    assert key_rows(initial)[0]['provider_message'] == 'First key finished'
    assert case.client.post('/api/tasks/' + task_id + '/retry').status_code == 409
    assert case.client.post('/api/tasks/' + task_id + '/reconcile').status_code == 200
    assert worker.run_once(client=object())
    final = state(case)
    assert final['status'] == 'needs_review' and final['model_results'] == initial['model_results']
    assert len(calls) == 2


def test_cancel_pending_channel_item_without_distribution_projects_all_keys_untested(db, channel_case, monkeypatch):
    case = channel_case
    four_keys(db, case)
    response = queue(case)
    assert response.status_code == 200, response.text
    assert case.client.post('/api/tasks/' + response.json()['id'] + '/cancel').status_code == 200
    result = state(case)
    assert result['status'] == 'cancelled' and result['total_key_count'] == 4
    assert result['completed_key_count'] == 0 and all(row['status'] == 'not_tested' for row in key_rows(result))
    monkeypatch.setattr(local_model_probe, 'probe_credential', lambda *a, **kw: pytest.fail('Never run a cancelled task'))
    assert not worker.run_once(client=object())
