"""Exercise frozen AWS credentials through the worker, using an in-memory remote."""
# ruff: noqa: F811
import json
from copy import deepcopy

import pytest
from app import worker
from app.adapters.channel_config import create_payload, validate_readback
from app.adapters.silicon import RemoteError
from app.models_channels import Channel, Distribution, Task, TaskItem
from app.security import decrypt
from test_aws_template_variants import (  # noqa: F401
    aws,
    create_template,
    no_external_calls,
    submit,
)


class MemoryAdapter:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.before_write = lambda: None

    def find_unique_name(self, name):
        return next((row for row in self.rows.values() if row['name'] == name), None)

    def detail(self, identifier):
        return self.rows[identifier]

    def create(self, **values):
        payload = create_payload(**values, supported_types={14, 33}, supports_proxy=True)
        self.before_write()
        self.writes.append(payload)
        identifier = str(100 + len(self.writes))
        self.rows[identifier] = {'id': identifier, **deepcopy(payload)}
        return identifier

    def validate_created_config(self, remote, config, proxy, channel_type):
        validate_readback(remote, config, proxy, channel_type, supported_types={14, 33}, supports_proxy=True)


def attach(monkeypatch):
    remote = MemoryAdapter()

    def factory(site, before_write=None):
        remote.before_write = before_write or (lambda: None)
        return remote

    monkeypatch.setattr(worker, 'get_adapter', factory)
    monkeypatch.setattr('app.routers.channels.notify_worker', lambda: None)
    return remote


def test_mixed_bedrock_records_freeze_distinct_modes_and_reconcile_without_duplicate(db, login, aws, monkeypatch):
    create_template(login('root'), aws)
    client = login('user')
    outcome = submit(client, aws, credentials='AKIA_LOCAL|secret-one|us-east-1\napi-private|eu-west-1')
    remote = attach(monkeypatch)
    items = [db.get(TaskItem, row['id']) for row in outcome['items']]
    for item in items:
        worker.execute_item(db, item)
        db.refresh(item)
        worker.execute_item(db, item)  # An uncertain response only reconciles the existing write.
    assert len(remote.writes) == 2
    assert {json.loads(row['settings'])['aws_key_type'] for row in remote.writes} == {'ak_sk', 'api_key'}
    assert {item.snapshot['wire_format_schema']['type'] for item in items} == {'aws_ak_sk', 'aws_api_key'}
    for item in items:
        channel = db.get(Channel, item.channel_id)
        assert channel.format_id == aws[1]['aws_bedrock'].id
        assert item.snapshot['format_schema']['type'] == 'aws_bedrock'
        dist = db.get(Distribution, item.distribution_id)
        bad = deepcopy(remote.detail(dist.remote_id))
        actual = json.loads(bad['settings'])['aws_key_type']
        bad['settings'] = json.dumps({'aws_key_type': 'api_key' if actual == 'ak_sk' else 'ak_sk'})
        with pytest.raises(RemoteError) as error:
            worker.validate_created(bad, item, dist, remote)
        assert error.value.unknown

    # Rotating a merged-format channel must not silently change its remote auth mode.
    task = db.get(Task, outcome['id'])
    task.status = 'succeeded'
    for item in items:
        item.status = 'succeeded'
    db.commit()
    iam = next(item for item in items if item.snapshot['wire_format_schema']['type'] == 'aws_ak_sk')
    before = decrypt(db.get(Channel, iam.channel_id).key_encrypted)
    response = client.post(f'/api/channels/{iam.channel_id}/rotate', json={'key': 'new-api-key|us-east-1'})
    assert response.status_code == 422 and '2 条' in response.json()['detail']
    response = client.post(f'/api/channels/{iam.channel_id}/rotate', json={
        'key': 'new-api-key|us-east-1\nnew-api-key-two|eu-west-1'})
    assert response.status_code == 422 and '认证' in response.json()['detail']
    assert decrypt(db.get(Channel, iam.channel_id).key_encrypted) == before


def test_claude_proxy_worker_uses_raw_key_and_verified_origin_in_type14(db, login, aws, monkeypatch):
    create_template(login('root'), aws, 'aws_claude')
    outcome = submit(login('user'), aws, 'aws_claude', api_base_url='https://customer-live.api.aws', inventory=True)
    remote = attach(monkeypatch)
    item = db.get(TaskItem, outcome['items'][0]['id'])
    worker.execute_item(db, item)
    assert len(remote.writes) == 1
    sent = remote.writes[0]
    assert sent['type'] == 14 and sent['status'] == 2
    assert sent['base_url'] == 'https://customer-live.api.aws'
    assert sent['key'] == 'proxy-private-key' and json.loads(sent['settings']) == {}
    dist = db.get(Distribution, item.distribution_id)
    wrong_origin = {**remote.detail(dist.remote_id), 'base_url': 'https://wrong-customer.api.aws'}
    with pytest.raises(RemoteError) as error:
        worker.validate_created(wrong_origin, item, dist, remote)
    assert error.value.unknown
