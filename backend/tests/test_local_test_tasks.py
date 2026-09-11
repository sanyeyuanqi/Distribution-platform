"""Local tests retain authorization, credential isolation and at-most-once sends."""
# ruff: noqa: F811 - imported pytest fixtures are injected as parameters
import pytest
from app import local_model_probe, worker
from app.credential_containers import bundle_identity, encode_entries, partition_entries
from app.db import utcnow
from app.models import CredentialFormat, Site
from app.models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
)
from app.security import encrypt
from sqlalchemy import func, select
from test_channel_monitoring import monitor_action, observed, row  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401


@pytest.mark.parametrize('remote_state', ['manual_disabled', 'automatic_disabled', 'site_changed'])
def test_uploaded_local_test_ignores_remote_enablement_and_seller_availability(db, observed, monkeypatch, remote_state):
    site = db.get(Site, observed.dist.site_id)
    if remote_state == 'site_changed':
        site.enabled, site.archived, site.adapter = False, True, 'unimplemented'
        site.base_url = 'https://different.example.invalid'
    else:
        observed.dist.remote_snapshot = {**observed.dist.remote_snapshot,
            'status': 2 if remote_state == 'manual_disabled' else 3}
    db.commit()
    assert row(observed)['local_test_available'] is True
    item, _ = monitor_action(db, observed)
    monkeypatch.setattr(worker, 'get_adapter', lambda *a, **kw: pytest.fail('Seller must not be contacted'))
    assert worker.run_once(client=object())
    state = row(observed)['connectivity_test']
    assert state['status'] == 'succeeded' and state['source'] == 'local'
    assert state['tested_count'] == 1 and state['passed_count'] == 1 and state['failed_count'] == 0
    assert state['task_id'] == item.task_id and observed.tests == ['test-model']


def test_seller_changes_between_queue_and_send_do_not_block_uploaded_local_probe(db, observed):
    monitor_action(db, observed)

    def change_remote():
        site = db.get(Site, observed.dist.site_id)
        site.enabled, site.archived, site.seller_user_id = False, True, 'changed'
        db.commit()

    observed.before_test = change_remote
    assert worker.run_once(client=object())
    assert row(observed)['connectivity_test']['status'] == 'succeeded'


def change_upload_evidence(db, observed, change):
    dist = observed.dist
    version = db.scalar(select(DistributionVersion).where(DistributionVersion.distribution_id == dist.id,
                                                          DistributionVersion.valid_to.is_(None)))
    if change == 'never_created':
        dist.remote_id, dist.status = None, 'failed'
    elif change in ('deleted', 'missing'):
        dist.status = change
    elif change == 'snapshot_mismatch':
        dist.remote_snapshot = {**dist.remote_snapshot, 'id': 'different'}
    elif change == 'local_removal':
        dist.remote_snapshot = {**dist.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    elif change == 'no_history':
        db.delete(version)
    elif change == 'closed_history':
        version.valid_to = utcnow()
    elif change == 'other_partition_history':
        other = db.scalar(select(Distribution).where(Distribution.channel_id == dist.channel_id,
                                                      Distribution.id != dist.id))
        version.distribution_id = other.id
    elif change == 'unuploaded_new_key':
        # Queue preparation may update both numeric versions before any write.
        observed.channel.key_version += 1
        dist.key_version = observed.channel.key_version
    else:
        raise AssertionError(change)
    db.commit()


@pytest.mark.parametrize('change', ['never_created', 'deleted', 'missing', 'snapshot_mismatch',
    'local_removal', 'no_history', 'closed_history', 'other_partition_history', 'unuploaded_new_key'])
def test_unconfirmed_upload_cannot_queue_or_project_as_testable(db, observed, change):
    change_upload_evidence(db, observed, change)
    state = row(observed)
    assert state['local_test_available'] is False and state['local_test_reason']
    before = db.scalar(select(func.count()).select_from(Task))
    response = observed.client.post('/api/channels/' + observed.channel.id + '/actions', json={
        'action': 'test', 'distribution_ids': [observed.dist.id], 'model': 'test-model'})
    assert response.status_code == 409, response.text
    assert db.scalar(select(func.count()).select_from(Task)) == before
    assert not observed.tests


@pytest.mark.parametrize('change', ['never_created', 'deleted', 'missing', 'snapshot_mismatch',
    'local_removal', 'no_history', 'closed_history', 'other_partition_history', 'unuploaded_new_key',
    'relinked', 'replaced_history'])
def test_upload_evidence_is_rechecked_immediately_before_request(db, observed, change):
    item, _ = monitor_action(db, observed)

    def mutate():
        if change == 'relinked':
            observed.dist.remote_id = '999'
            observed.dist.remote_snapshot = {**observed.dist.remote_snapshot, 'id': 999}
            db.commit()
        elif change == 'replaced_history':
            previous = db.scalar(select(DistributionVersion).where(
                DistributionVersion.distribution_id == observed.dist.id))
            previous.valid_to = utcnow()
            db.add(DistributionVersion(distribution_id=observed.dist.id, key_version=1))
            db.commit()
        else:
            change_upload_evidence(db, observed, change)

    observed.before_test = mutate
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled'
    assert not observed.tests


@pytest.mark.parametrize('change', ['key', 'config', 'proxy', 'format'])
def test_changed_local_inputs_stop_before_any_request(db, observed, change):
    item, _ = monitor_action(db, observed)

    def mutate():
        channel = db.get(Channel, observed.channel.id)
        if change == 'key':
            channel.key_encrypted = encrypt('changed-key')
        elif change == 'config':
            observed.dist.template_snapshot = {'effective_channel_config': {'model_mapping': {'test-model': 'changed'}}}
        elif change == 'proxy':
            channel.proxy_encrypted = encrypt('http://changed.example:8080')
        else:
            fmt = db.get(CredentialFormat, channel.format_id)
            fmt.version = '2'
        db.commit()

    observed.before_test = mutate
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled'
    assert not observed.tests


def make_container(db, case):
    channel = db.get(Channel, case.channel.id)
    fmt = db.get(CredentialFormat, channel.format_id)
    entries = [{'key': 'sk-local-one', 'remark': '', 'proxy': ''},
               {'key': 'sk-local-two', 'remark': '', 'proxy': ''}]
    channel.key_mode, channel.key_count = 'multiple', 2
    channel.key_encrypted = encrypt(encode_entries(entries))
    channel.fingerprint = bundle_identity(entries, fmt)
    version = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id))
    version.key_encrypted, version.fingerprint = channel.key_encrypted, channel.fingerprint
    part = partition_entries(entries, fmt)[0]
    case.dist.partition_key, case.dist.key_count = part['partition_key'], 2
    db.commit()
    return entries


def test_every_container_key_is_probed_and_partial_failure_is_visible(db, observed, monkeypatch):
    entries = make_container(db, observed)
    calls = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        calls.append(key)
        return {'success': len(calls) == 1, 'latency_ms': 9, 'message': 'DO NOT STORE KEY'}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    item, _ = monitor_action(db, observed)
    assert worker.run_once(client=object())
    state = row(observed)['connectivity_test']
    assert calls == [entry['key'] for entry in entries]
    assert state['status'] == 'failed' and state['tested_count'] == 2
    assert state['passed_count'] == 1 and state['failed_count'] == 1 and state['latency_ms'] == 18
    assert 'DO NOT STORE' not in str(state)
    assert observed.client.post('/api/tasks/' + item.task_id + '/retry').status_code == 409


def test_cancellation_between_oauth_and_inference_stops_second_request(db, observed, monkeypatch):
    item, _ = monitor_action(db, observed)
    requests = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        requests.append('oauth')
        # Another session cancels after the first response; the next guard must
        # reload cancellation before any potentially billable inference.
        db.get(Task, item.task_id).cancelled = True
        db.commit()
        before_request()
        requests.append('inference')
        return {'success': True}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    assert requests == ['oauth']
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled'


@pytest.mark.parametrize('boundary', ['oauth', 'next_key'])
def test_upload_confirmation_revoked_after_first_request_stops_remaining_requests(db, observed, monkeypatch, boundary):
    if boundary == 'next_key':
        make_container(db, observed)
    item, _ = monitor_action(db, observed)
    requests = []

    def probe(key, schema, model, config, proxy='', before_request=None):
        before_request()
        requests.append(key)
        version = db.scalar(select(DistributionVersion).where(
            DistributionVersion.distribution_id == observed.dist.id))
        version.valid_to = utcnow()
        db.commit()
        if boundary == 'oauth':
            before_request()
            requests.append('inference')
        return {'success': True}

    monkeypatch.setattr(local_model_probe, 'probe_credential', probe)
    assert worker.run_once(client=object())
    assert len(requests) == 1
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled'


def test_legacy_unsent_remote_test_requires_explicit_new_local_test(db, observed):
    item, _ = monitor_action(db, observed)
    item.snapshot = {k: v for k, v in item.snapshot.items() if k != 'test_source'}
    db.commit()
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item.id).status == 'cancelled' and not observed.tests
