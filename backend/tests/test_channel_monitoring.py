"""Scoped, durable observations never become billing facts or replay model tests."""
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.channel_observation import extract_usage
from app.adapters.silicon import RemoteError
from app.channel_service import new_task
from app.db import uid, utcnow
from app.distribution_monitoring import observation_json
from app.models import Site, User
from app.models_billing import UsageFact
from app.models_channels import Distribution, DistributionVersion, Task, TaskItem
from sqlalchemy import func, select
from test_channels import (  # noqa: F401 - shared isolated fixture setup
    delete_case,
    setup_catalog,
)


@pytest.fixture
def observed(request, monkeypatch, db):
    state = request.getfixturevalue('delete_case')
    # Monitoring uses a confirmed upload, while delete_case deliberately starts
    # from a missing remote channel and does not run the creation worker.
    state.dist.status = 'disabled'
    db.add(DistributionVersion(distribution_id=state.dist.id, key_version=state.channel.key_version))
    db.commit()
    state.tests, state.reads = [], []
    state.outcome = {'success': True, 'latency_ms': 31, 'message': 'provider-secret-must-not-persist'}
    state.test_error, state.usage_error = None, None
    state.on_test, state.before_test, state.on_usage = lambda: None, lambda: None, lambda: None
    state.usage_value = extract_usage({'used_quota': 0, 'balance': 12.5, 'balance_updated_time': 100})

    class ObservingAdapter:
        def __init__(self, site, before_write=None):
            self.before_write = before_write

        def test_channel(self, *args, **kwargs):
            pytest.fail('Local tests must never call the seller test endpoint')

        def usage(self, remote_id, *, expected):
            assert expected['id'] == remote_id
            state.reads.append(remote_id)
            state.on_usage()
            if state.usage_error:
                raise state.usage_error
            return dict(state.usage_value)

    monkeypatch.setattr(worker, 'get_adapter', ObservingAdapter)
    from app import local_model_probe

    def probe(key, schema, model, channel_config, proxy='', before_request=None):
        state.before_test()
        before_request()
        state.tests.append(model)
        state.on_test()
        if state.test_error:
            raise state.test_error
        return dict(state.outcome)

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    return state


def monitor_action(db, case, operation='test', **overrides):
    values = {'action': operation, 'site_ids': [case.dist.site_id]}
    if operation == 'test':
        values.update(model='test-model', idempotency_key=uid())
    values.update(overrides)
    if operation == 'sync_usage':
        # Preserve execution coverage for historical jobs queued before the
        # manual sync API was retired; new clients cannot create these jobs.
        from app.routers.channels import existing_operation
        db.refresh(case.dist)
        db.refresh(case.channel)
        actor = db.get(User, case.channel.owner_id)
        task = existing_operation(db, actor, case.channel, operation, [case.dist])
        db.commit()
        return db.scalar(select(TaskItem).where(TaskItem.task_id == task.id)), values
    response = case.client.post('/api/channels/' + case.channel.id + '/actions', json=values)
    assert response.status_code == 200, response.text
    return db.get(TaskItem, response.json()['items'][0]['id']), values


def row(case, client=None):
    response = (client or case.client).get('/api/channels/' + case.channel.id)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()['distributions'] if item['id'] == case.dist.id)


@pytest.mark.parametrize('changes', [
    {'model': None}, {'model': ''}, {'model': 'model-from-another-site'},
    {'site_ids': []}, {'site_ids': None}, {'site_ids': ['unknown-site']},
    {'site_ids': ['one', 'two']},
])
def test_model_test_requires_one_owned_distribution_and_its_model(db, observed, changes):
    before = db.scalar(select(func.count()).select_from(Task))
    values = {'action': 'test', 'site_ids': [observed.dist.site_id], 'model': 'test-model', **changes}
    response = observed.client.post('/api/channels/' + observed.channel.id + '/actions', json=values)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert not observed.tests


def test_admin_can_see_pending_running_and_final_result_without_task_page_permissions(db, login, observed):
    admin = login('admin')
    values = {'action': 'test', 'site_ids': [observed.dist.site_id], 'model': 'test-model'}
    path = '/api/channels/' + observed.channel.id + '/actions'
    assert login('other_admin').post(path, json=values).status_code == 404
    assert login('other_user').post(path, json=values).status_code == 404
    response = admin.post(path, json=values)
    assert response.status_code == 200, response.text
    task_id = response.json()['id']
    assert admin.get('/api/tasks/' + task_id).status_code == 403
    assert row(observed, admin)['connectivity_test']['status'] == 'pending'
    during = []
    observed.on_test = lambda: during.append(row(observed, admin)['connectivity_test'])
    assert worker.run_once(client=object())
    state = row(observed, admin)['connectivity_test']
    assert during[0]['status'] == 'running' and during[0]['task_id'] == task_id
    assert state['status'] == 'succeeded' and state['task_id'] == task_id
    assert state['model'] == 'test-model' and state['latency_ms'] == 31 and state['tested_at'].endswith('Z')
    assert 'provider-secret' not in str(state)
    assert len(observed.tests) == 1


def test_test_submission_idempotency_recovers_pending_and_finished_response_without_new_probe(db, observed):
    item, values = monitor_action(db, observed)
    path = '/api/channels/' + observed.channel.id + '/actions'
    assert observed.client.post(path, json=values).json()['id'] == item.task_id
    assert observed.client.post(path, json={**values, 'model': 'different'}).status_code == 409
    assert worker.run_once(client=object())
    assert observed.client.post(path, json=values).json()['id'] == item.task_id
    assert len(observed.tests) == 1
    assert db.scalar(select(func.count()).select_from(Task).where(Task.kind == 'test')) == 1


def test_failed_test_is_visible_but_cannot_be_replayed_by_task_retry(db, observed):
    observed.outcome = {'success': False, 'message': 'secret-provider-response'}
    item, _ = monitor_action(db, observed)
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'failed'
    state = row(observed)['connectivity_test']
    assert state['status'] == 'failed' and not state['success'] and 'secret' not in state['message']
    assert observed.client.post('/api/tasks/' + item.task_id + '/retry').status_code == 409
    assert len(observed.tests) == 1


def test_unknown_test_never_repeats_after_recovery_and_new_explicit_test_is_independent(db, observed):
    observed.test_error = RemoteError('测试请求结果未知', unknown=True)
    item, values = monitor_action(db, observed)
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).stage == 'test_sent'
    assert row(observed)['connectivity_test']['status'] == 'needs_review'
    assert observed.client.post('/api/tasks/' + item.task_id + '/retry').status_code == 409
    assert observed.client.post('/api/tasks/' + item.task_id + '/reconcile').status_code == 200
    assert worker.run_once(client=object())
    assert len(observed.tests) == 1
    observed.test_error = None
    fresh, _ = monitor_action(db, observed)
    assert fresh.task_id != item.task_id
    assert worker.run_once(client=object())
    assert len(observed.tests) == 2
    assert row(observed)['connectivity_test']['task_id'] == fresh.task_id
    # An old read-only reconciliation cannot overwrite the newer successful test.
    assert observed.client.post('/api/tasks/' + item.task_id + '/reconcile').status_code == 200
    assert worker.run_once(client=object())
    assert row(observed)['connectivity_test']['task_id'] == fresh.task_id
    assert row(observed)['connectivity_test']['status'] == 'succeeded'
    assert len(observed.tests) == 2
    assert observed.client.post('/api/channels/' + observed.channel.id + '/actions', json=values).json()['id'] == item.task_id


@pytest.mark.parametrize('operation', ['test', 'sync_usage'])
def test_cancelled_queued_monitor_is_immediately_terminal_without_worker(db, observed, operation):
    item, _ = monitor_action(db, observed, operation)
    assert observed.client.post('/api/tasks/' + item.task_id + '/cancel').status_code == 200
    name = 'connectivity_test' if operation == 'test' else 'usage_sync'
    assert row(observed)[name]['status'] == 'cancelled'
    assert not worker.run_once(client=object())
    assert not observed.tests and not observed.reads


@pytest.mark.parametrize('change', ['session', 'models', 'association'])
def test_test_authorization_and_model_scope_are_rechecked_before_costly_request(db, users, observed, change):
    item, _ = monitor_action(db, observed)

    def revoke():
        if change == 'session':
            users['user'].session_version += 1
        elif change == 'models':
            observed.dist.models = ['another-model']
        else:
            observed.dist.partition_key = 'changed-partition'
        db.commit()

    observed.before_test = revoke
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == 'cancelled' and not item.remote_write_attempted
    assert observation_json(db.get(Distribution, observed.dist.id), 'test')['status'] == 'cancelled'
    assert not observed.tests


def test_usage_snapshot_preserves_zero_and_does_not_create_usage_or_settlement_facts(db, users, observed):
    fact = UsageFact(source_id='verified-monitor-fixture', site_id=observed.dist.site_id,
        distribution_id=observed.dist.id, channel_id=observed.channel.id, owner_id=users['user'].id,
        admin_id=users['admin'].id, category_id=observed.channel.category_id, occurred_at=utcnow(),
        raw_amount=8, raw_unit='USD', amount=8, unit='USD', conversion_version='fixture', verified=True)
    db.add(fact)
    db.commit()
    item, _ = monitor_action(db, observed, 'sync_usage')
    assert worker.run_once(client=object())
    result = row(observed)
    state = result['usage_sync']
    assert state['status'] == 'succeeded' and state['task_id'] == item.task_id
    assert state['remote_usage'] == observed.usage_value
    assert state['remote_usage']['used_quota'] == 0 and not state['remote_usage']['settlement_verified']
    assert result['usage_by_unit'] == {'USD': '8.00000000'}
    assert db.scalar(select(func.count()).select_from(UsageFact)) == 1
    assert not observed.tests


def test_failed_usage_refresh_retains_previous_snapshot_and_timestamp(db, observed):
    first, _ = monitor_action(db, observed, 'sync_usage')
    assert worker.run_once(client=object())
    previous = row(observed)['usage_sync']
    observed.usage_error = RemoteError('远端读取权限不足', category='permission_denied')
    second, _ = monitor_action(db, observed, 'sync_usage')
    assert second.task_id != first.task_id
    assert row(observed)['usage_sync']['remote_usage'] == previous['remote_usage']
    assert worker.run_once(client=object())
    current = row(observed)['usage_sync']
    assert current['status'] == 'failed' and current['task_id'] == second.task_id
    assert current['remote_usage'] == previous['remote_usage'] and current['synced_at'] == previous['synced_at']


@pytest.mark.parametrize('change', ['cancel', 'association'])
def test_usage_rechecks_cancel_and_association_after_remote_read(db, observed, change):
    item, _ = monitor_action(db, observed, 'sync_usage')

    def mutate():
        if change == 'cancel':
            db.get(Task, item.task_id).cancelled = True
        else:
            observed.dist.remote_id = '999'
        db.commit()

    observed.on_usage = mutate
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled'
    state = observation_json(db.get(Distribution, observed.dist.id), 'sync_usage')
    assert state['status'] == 'cancelled' and state['remote_usage'] is None


def test_retry_cannot_queue_old_unsent_test_while_new_test_is_pending(db, observed):
    first, _ = monitor_action(db, observed)
    first.status = 'failed'
    db.get(Task, first.task_id).status = 'failed'
    db.commit()
    second, _ = monitor_action(db, observed)
    assert observed.client.post('/api/tasks/' + first.task_id + '/retry').status_code == 409
    assert row(observed)['connectivity_test']['task_id'] == second.task_id
    assert not observed.tests


def test_site_sync_preserves_manual_task_identity_and_pending_observations(db, users, observed):
    item, _ = monitor_action(db, observed, 'sync_usage')
    assert worker.run_once(client=object())
    site = db.get(Site, observed.dist.site_id)
    task = new_task(db, users['user'], users['user'].id, 'sync', snapshot={'owner_ids': [users['user'].id]})
    sync_item = TaskItem(task_id=task.id, site_id=site.id, operation='sync',
                         snapshot={'owner_ids': [users['user'].id]})
    db.add(sync_item)
    db.commit()
    remote = {**observed.remote, 'used_quota': 44, 'balance': 1}
    adapter = SimpleNamespace(channels=lambda: [remote])
    worker.sync_site(db, adapter, task, sync_item, users['user'], site)
    db.commit()
    result = row(observed)['usage_sync']
    assert result['task_id'] == item.task_id and result['status'] == 'succeeded'
    assert result['remote_usage']['used_quota'] == 44
    # A fresh manual pending job must survive another site-wide synchronization.
    newer, _ = monitor_action(db, observed, 'sync_usage')
    worker.sync_site(db, adapter, task, sync_item, users['user'], site)
    db.commit()
    pending = row(observed)['usage_sync']
    assert pending['task_id'] == newer.task_id and pending['status'] == 'pending'
    assert pending['remote_usage']['used_quota'] == 44


def test_manual_usage_actions_are_unavailable_to_all_users(db, login, observed):
    values = {'action': 'sync_usage', 'site_ids': [observed.dist.site_id]}
    path = '/api/channels/' + observed.channel.id + '/actions'
    assert login('other_user').post(path, json=values).status_code == 422
    assert observed.client.post(path, json=values).status_code == 422
    assert observed.client.post(path, json={**values, 'model': 'test-model'}).status_code == 422
    assert observed.client.post(path, json={**values, 'site_ids': None}).status_code == 422
    assert observed.client.post(path, json={**values, 'site_ids': [uid()]}).status_code == 422
    assert not observed.reads
