"""One local batch, real remote containers, and independently authorized partitions."""
# ruff: noqa: F811
import json

import pytest
from app import worker
from app.adapters.channel_config import create_payload, validate_readback
from app.channel_service import refresh_task
from app.credential_containers import channel_entries
from app.db import uid
from app.models import Category, CredentialFormat, Site
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    KeyVersion,
    Task,
    TaskItem,
)
from app.security import decrypt
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401


@pytest.fixture
def container_site(db, catalog_setup):
    catalog_setup.capabilities = {**catalog_setup.capabilities, 'multi_key': 'supported',
        'vertex_claude_api_key': 'supported', 'custom_models': True, 'can_toggle': True,
        'test': 'supported', 'usage': 'supported'}
    db.commit()
    return catalog_setup


def setup_template(db, root, site, family='AWS', kind='aws_bedrock', models=None):
    category = db.scalar(select(Category).where(Category.family == family))
    fmt = next(row for row in db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
               if row.schema_config['type'] == kind)
    response = root.post('/api/upload-templates', json={'site_id': site.id, 'category_id': category.id,
        'format_id': fmt.id, 'models': models or ['fixture-model'], 'routing_group': 'default', 'enabled': True})
    assert response.status_code == 201, response.text
    return category, fmt


def request_body(category, fmt, text, **values):
    return {'category_id': category.id, 'format_id': fmt.id, 'credentials': text,
            'idempotency_key': 'multi-fixture-request-001', **values}


def post(client, body):
    response = client.post('/api/uploads/simple-submit', json=body)
    assert response.status_code == 200, response.text
    return response.json()


def complete(db, result):
    for row in result['items']:
        item = db.get(TaskItem, row['id'])
        worker.execute_item(db, item)
        item.status, item.stage = 'succeeded', 'complete'
        refresh_task(db, db.get(Task, item.task_id))
        db.commit()


@pytest.fixture
def remotes(monkeypatch):
    records, writes = {}, []
    class Fake:
        def __init__(self, site, before_write):
            self.site, self.before_write = site, before_write

        def find_unique_name(self, name):
            return next((row for (site_id, _), row in records.items()
                         if site_id == self.site.id and row['name'] == name), None)

        def create(self, **values):
            payload = create_payload(**values, supported_types={1, 3, 14, 24, 33, 41}, supports_proxy=True)
            if values.get('keys'):
                payload['channel_info'] = {'is_multi_key': True, 'multi_key_size': len(values['keys']), 'multi_key_mode': 'random'}
            self.before_write()
            remote_id = str(len(records) + 1)
            records[(self.site.id, remote_id)] = {'id': int(remote_id), **payload}
            writes.append({'operation': 'create', 'site_id': self.site.id, **payload})
            return remote_id

        def detail(self, remote_id):
            return records[(self.site.id, str(remote_id))]

        def validate_created_config(self, remote, *, config, proxy, channel_type):
            validate_readback(remote, config, proxy, channel_type, supported_types={1, 3, 14, 24, 33, 41}, supports_proxy=True)

        def edit(self, remote_id, *, changes, key=None, expected=None, multikey=False):
            current = self.detail(remote_id)
            assert all(current.get(k) == value for k, value in (expected or {}).items())
            self.before_write()
            current.update(changes)
            if key is not None:
                current['key'] = key
            writes.append({'operation': 'rotate', 'key': key, 'multikey': multikey})
    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write: Fake(site, before_write))
    return records, writes


AWS_KEYS = 'AKIA_one|secret-one|us-east-1\nAKIA_two|secret-two|us-east-1\napi-key-one\napi-key-two'


def test_mixed_aws_is_one_local_channel_and_two_real_random_containers(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    body = request_body(category, fmt, AWS_KEYS, models=['fixture-model', 'other-site-only'])
    preview = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'}).json()
    assert preview['can_submit'] and preview['channel_count'] == 1 and preview['remote_channel_count'] == 2
    result = post(client, body)
    assert len(result['items']) == 2 and len({row['channel_id'] for row in result['items']}) == 1
    channel = db.scalar(select(Channel))
    assert channel.key_mode == 'multiple' and channel.key_count == 4
    assert len(channel_entries(channel)) == 4 and 'secret-one' not in channel.key_encrypted
    assert db.scalar(select(func.count()).select_from(ChannelCredential)) == 4
    distributions = list(db.scalars(select(Distribution)))
    assert len(distributions) == 2 and {row.key_count for row in distributions} == {2}
    assert len({row.remote_name for row in distributions}) == 2
    for row in result['items']:
        snapshot = db.get(TaskItem, row['id']).snapshot
        assert snapshot['models'] == ['fixture-model']
        assert len(snapshot['member_fingerprints']) == 2
        assert all(key not in json.dumps(snapshot) for key in ('secret-one', 'api-key-one'))
    complete(db, result)
    writes = remotes[1]
    assert len(writes) == 2 and all(row['channel_info']['multi_key_size'] == 2 for row in writes)
    assert {json.loads(row['settings'])['aws_key_type'] for row in writes} == {'ak_sk', 'api_key'}
    assert all(len(row['key'].splitlines()) == 2 for row in writes)
    assert post(client, body)['id'] == result['id']
    again = client.post('/api/uploads/simple-submit', json={**body, 'idempotency_key': 'multi-repeat-0002'})
    assert again.status_code == 422
    overlap = client.post('/api/uploads/simple-submit', json={**body, 'credentials': 'api-key-one\nnew-api-key',
                                                            'idempotency_key': 'multi-overlap-0003'})
    assert overlap.status_code == 422 and db.scalar(select(func.count()).select_from(Channel)) == 1


def test_each_remote_partition_is_selected_by_distribution_id(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    result = post(client, request_body(category, fmt, AWS_KEYS))
    complete(db, result)
    channel = db.scalar(select(Channel))
    distributions = list(db.scalars(select(Distribution).order_by(Distribution.id)))
    url = f'/api/channels/{channel.id}/actions'
    response = client.post(url, json={'action': 'test', 'site_ids': [container_site.id], 'model': 'fixture-model'})
    assert response.status_code == 422
    selected = client.post(url, json={'action': 'test', 'distribution_ids': [distributions[0].id],
                                    'model': 'fixture-model', 'idempotency_key': 'one-partition-test'})
    assert selected.status_code == 200, selected.text
    assert len(selected.json()['items']) == 1
    item = db.get(TaskItem, selected.json()['items'][0]['id'])
    assert item.distribution_id == distributions[0].id
    assert client.post('/api/tasks/' + selected.json()['id'] + '/cancel').status_code == 200
    bad = client.post(url, json={'action': 'delete_remote', 'distribution_ids': [uid()], 'confirmation': 'DELETE 1'})
    assert bad.status_code == 422
    deleted = client.post(url, json={'action': 'delete_remote', 'distribution_ids': [distributions[1].id], 'confirmation': 'DELETE 1'})
    assert deleted.status_code == 200 and len(deleted.json()['items']) == 1
    assert db.get(TaskItem, deleted.json()['items'][0]['id']).distribution_id == distributions[1].id
    assert len(remotes[1]) == 2  # API only enqueues; no real delete executed.


def test_whole_batch_rotation_preserves_partitions_versions_and_member_uniqueness(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    result = post(client, request_body(category, fmt, AWS_KEYS))
    complete(db, result)
    channel = db.scalar(select(Channel))
    original = channel.key_encrypted
    bad = client.post(f'/api/channels/{channel.id}/rotate', json={'key': 'only-one-key'})
    assert bad.status_code == 422
    changed = 'AKIA_new-one|new-secret-one|us-east-1\nAKIA_new-two|new-secret-two|us-east-1\nnew-api-one\nnew-api-two'
    response = client.post(f'/api/channels/{channel.id}/rotate', json={'key': changed})
    assert response.status_code == 200, response.text
    task = client.get('/api/tasks/' + response.json()['task_id']).json()
    complete(db, task)
    assert len(remotes[1]) == 4 and all(row['multikey'] for row in remotes[1][2:])
    versions = list(db.scalars(select(KeyVersion).order_by(KeyVersion.version)))
    assert len(versions) == 2 and versions[0].key_encrypted == original
    assert channel.key_count == 4 and channel.key_version == 2
    assert db.scalar(select(func.count()).select_from(ChannelCredential)) == 4
    assert 'new-api-one' in decrypt(channel.key_encrypted)
    assert '"container"' not in client.post(f'/api/channels/{channel.id}/reveal').json()['key']


def test_azure_resource_partitions_and_vertex_per_site_contracts(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site, 'Azure', 'azure_gpt')
    result = post(client, request_body(category, fmt, 'one-resource|key-a|2025-04-01-preview\none-resource|key-b|2025-04-01-preview\ntwo-resource|key-c|2025-04-01-preview'))
    complete(db, result)
    assert db.scalar(select(func.count()).select_from(Channel)) == 1
    assert sorted(row.key_count for row in db.scalars(select(Distribution))) == [1, 2]
    assert {row['base_url'] for row in remotes[1]} == {'https://one-resource.openai.azure.com', 'https://two-resource.openai.azure.com'}
    assert all('resource|' not in row['key'] for row in remotes[1])
    category, fmt = setup_template(db, root, container_site, 'Google', 'vertex_gemini', ['gemini-2.5-flash'])
    fork = Site(id=uid(), name='Seller fork', prefix='fork', base_url='https://fork.invalid',
        seller_user_id='1', token_encrypted=container_site.token_encrypted, adapter='silicon-v1',
        enabled=True, health='healthy', verified_at=container_site.verified_at, capabilities=dict(container_site.capabilities))
    db.add(fork)
    db.commit()
    setup_template(db, root, fork, 'Google', 'vertex_gemini', ['gemini-2.5-flash'])
    account = {'project_id': 'project-one', 'client_email': 'one@example.com', 'private_key': 'private-one'}
    accounts = [account, {**account, 'project_id': 'project-two', 'private_key': 'private-two'}, 'vertex-one', 'vertex-two']
    result = post(client, request_body(category, fmt, json.dumps(accounts), idempotency_key='vertex-container-001'))
    assert len(result['items']) == 5  # Official: JSON+API containers. Seller: JSON+two API channels.
    assert len({row['channel_id'] for row in result['items']}) == 1
    complete(db, result)
    json_writes = [row for row in remotes[1] if row['type'] == 41 and json.loads(row['settings'])['vertex_key_type'] == 'json']
    assert len(json_writes) == 2 and all(isinstance(json.loads(row['key'])[0], dict) for row in json_writes)


def test_unverified_multikey_support_and_tampered_members_never_write(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    container_site.capabilities = {**container_site.capabilities, 'multi_key': 'unknown'}
    db.commit()
    body = request_body(category, fmt, 'key-one\nkey-two')
    assert client.post('/api/uploads/simple-submit', json=body).status_code == 422
    assert db.scalar(select(func.count()).select_from(Channel)) == 0
    container_site.capabilities = {**container_site.capabilities, 'multi_key': 'supported'}
    db.commit()
    result = post(client, body)
    item = db.get(TaskItem, result['items'][0]['id'])
    item.snapshot = {**item.snapshot, 'member_fingerprints': ['changed']}
    db.commit()
    with pytest.raises(worker.WriteStopped, match='成员'):
        worker.execute_item(db, item)
    assert not remotes[1]


def test_supplement_creates_missing_partitions_once_and_preserves_original_container(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    first = post(client, request_body(category, fmt, AWS_KEYS))
    complete(db, first)
    channel = db.scalar(select(Channel))
    original = (channel.id, channel.key_encrypted, channel.fingerprint)
    second = Site(id=uid(), name='Second site', prefix='second', base_url='https://second.invalid',
        seller_user_id='1', token_encrypted=container_site.token_encrypted, adapter='new-api-v1',
        enabled=True, health='healthy', verified_at=container_site.verified_at, capabilities=dict(container_site.capabilities))
    db.add(second)
    db.commit()
    setup_template(db, root, second)
    response = client.post(f'/api/channels/{channel.id}/actions', json={'action': 'redistribute', 'site_ids': [second.id]})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == 2
    complete(db, response.json())
    assert (channel.id, channel.key_encrypted, channel.fingerprint) == original
    assert db.scalar(select(func.count()).select_from(Channel)) == 1
    assert db.scalar(select(func.count()).select_from(Distribution)) == 4
    assert len(remotes[1]) == 4
    repeated = client.post(f'/api/channels/{channel.id}/actions', json={'action': 'redistribute', 'site_ids': [second.id]})
    assert repeated.status_code == 422 and len(remotes[1]) == 4


def test_reprepare_preserves_per_key_notes_proxy_and_frozen_partition(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site, 'OpenAI', 'api_key')
    result = post(client, request_body(category, fmt, 'key-one\nkey-two\nkey-three',
        remarks='first\nfirst\nthird', proxies='http://proxy-one.invalid:8080\nhttp://proxy-one.invalid:8080\nhttp://proxy-two.invalid:8080'))
    assert len(result['items']) == 2
    old_parts = {db.get(TaskItem, row['id']).snapshot['partition_key'] for row in result['items']}
    cancelled = client.post('/api/tasks/' + result['id'] + '/cancel')
    assert cancelled.status_code == 200
    rebuilt = client.post('/api/tasks/' + result['id'] + '/reprepare-templates')
    assert rebuilt.status_code == 200, rebuilt.text
    assert {db.get(TaskItem, row['id']).snapshot['partition_key'] for row in rebuilt.json()['items']} == old_parts
    complete(db, rebuilt.json())
    assert {row['remark'] for row in remotes[1]} == {'first', 'third'}
    assert {json.loads(row['setting'])['proxy'] for row in remotes[1]} == {'http://proxy-one.invalid:8080', 'http://proxy-two.invalid:8080'}


def test_rotation_stops_on_remote_auth_drift_and_misbound_partition(db, login, container_site, remotes):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    result = post(client, request_body(category, fmt, AWS_KEYS))
    complete(db, result)
    channel = db.scalar(select(Channel))
    response = client.post(f'/api/channels/{channel.id}/rotate', json={'key':
        'AKIA_new|new-secret|us-east-1\nAKIA_second|second-secret|us-east-1\nnew-api-one\nnew-api-two'})
    assert response.status_code == 200, response.text
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == response.json()['task_id'])))
    iam = next(item for item in items if item.snapshot['wire_format_schema']['type'] == 'aws_ak_sk')
    dist = db.get(Distribution, iam.distribution_id)
    remotes[0][(container_site.id, dist.remote_id)]['settings'] = '{"aws_key_type":"api_key"}'
    with pytest.raises(worker.RemoteError, match='认证方式'):
        worker.execute_item(db, iam)
    assert len(remotes[1]) == 2
    iam.distribution_id = next(item.distribution_id for item in items if item.id != iam.id)
    db.commit()
    with pytest.raises(worker.WriteStopped, match='分区'):
        worker.execute_item(db, iam)
    assert len(remotes[1]) == 2


@pytest.mark.parametrize('change', ['deleted', 'models'])
def test_repeat_never_recreates_deleted_or_overrides_changed_partitions(db, login, container_site, remotes, change):
    root, client = login('root'), login('user')
    category, fmt = setup_template(db, root, container_site)
    body = request_body(category, fmt, AWS_KEYS)
    result = post(client, body)
    complete(db, result)
    dist = db.scalar(select(Distribution))
    if change == 'deleted':
        dist.status = 'deleted'
    else:
        dist.models = ['manually-changed']
    db.commit()
    response = client.post('/api/uploads/simple-preview', json={k: v for k, v in body.items() if k != 'idempotency_key'})
    assert response.status_code == 200 and not response.json()['can_submit']
    assert response.json()['conflict_count'] == 4
    assert len(remotes[1]) == 2
