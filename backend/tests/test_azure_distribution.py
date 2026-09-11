"""Azure resource credentials are split into safe, per-Key channel payloads."""
# ruff: noqa: F811
import json

import pytest
from app import worker
from app.adapters.channel_config import create_payload, validate_readback
from app.db import uid
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.models_channels import Channel, Distribution, Task, TaskItem
from app.security import decrypt, encrypt
from sqlalchemy import select
from test_catalog_policy import catalog_setup, template_body  # noqa: F401


def setup_azure(db, root, site, *, legacy=False, kind='azure_gpt'):
    category = db.scalar(select(Category).where(Category.family == 'Azure'))
    fmt = next(row for row in db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
               if row.schema_config['type'] == kind)
    if legacy:
        fmt = CredentialFormat(id=uid(), category_id=category.id, code='newapi-3-api-key-v1', version='1',
            name='Historical Azure key', schema_config={'type': 'api_key', 'remote_type': 3}, enabled=True)
        db.add(fmt)
        db.flush()
        template = SiteUploadTemplate(id=uid(), **template_body(category, fmt, site, enabled=True),
            variant='azure_gpt', channel_config={'status': 2})
        db.add(template)
        db.commit()
        return category, fmt, template
    response = root.post('/api/upload-templates', json=template_body(category, fmt, site, enabled=True))
    assert response.status_code == 201, response.text
    return category, fmt, db.get(SiteUploadTemplate, response.json()['id'])


def attach(monkeypatch):
    records, writes = {}, []

    class FakeAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return next((row for row in records.values() if row['name'] == name), None)

        def create(self, **values):
            self.before_write()
            payload = create_payload(**values, supported_types={3, 14})
            remote_id = str(len(records) + 1)
            records[remote_id] = {'id': int(remote_id), **payload}
            writes.append(payload)
            return remote_id

        def detail(self, remote_id):
            return records[str(remote_id)]

        def validate_created_config(self, remote, *, config, proxy, channel_type):
            validate_readback(remote, config, proxy, channel_type, supported_types={3, 14})

        def edit(self, remote_id, *, changes, key, expected):
            assert expected['base_url'] == records[str(remote_id)]['base_url']
            assert expected['other'] == records[str(remote_id)]['other']
            self.before_write()
            records[str(remote_id)].update(changes)
            if key:
                records[str(remote_id)]['key'] = key
                writes.append({'key': key})

    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write: FakeAdapter(site, before_write))
    return records, writes


@pytest.mark.parametrize(('kind', 'credentials', 'urls', 'versions'), [
    ('azure_gpt', 'resource-one|test-key-one|2024-10-21\nresource-two|test-key-two|2025-04-01-preview',
     ['https://resource-one.openai.azure.com', 'https://resource-two.openai.azure.com'], ['2024-10-21', '2025-04-01-preview']),
    ('azure_claude', 'resource-one|test-key-one\nresource-two|test-key-two',
     ['https://resource-one.services.ai.azure.com/anthropic', 'https://resource-two.services.ai.azure.com/anthropic'], ['', '']),
])
def test_per_key_resources_are_frozen_and_only_api_key_is_sent(db, login, catalog_setup, monkeypatch, kind, credentials, urls, versions):
    category, fmt, _ = setup_azure(db, login('root'), catalog_setup, kind=kind)
    response = login('user').post('/api/uploads/simple-submit', json={'category_id': category.id, 'format_id': fmt.id,
        'credentials': credentials, 'models': ['fixture-model', 'outside-template'], 'idempotency_key': 'azure-batch-fixture'})
    assert response.status_code == 200, response.text
    assert 'test-key' not in response.text and response.json()['total'] == 2
    records, writes = attach(monkeypatch)
    for index, row in enumerate(response.json()['items']):
        item = db.get(TaskItem, row['id'])
        from app.credential_containers import channel_entries
        assert channel_entries(db.get(Channel, item.channel_id))[index]['key'] == credentials.splitlines()[index]
        assert item.snapshot['models'] == ['fixture-model']
        assert item.snapshot['channel_config']['base_url'] == urls[index]
        assert item.snapshot['channel_config']['other'] == versions[index]
        assert 'test-key' not in json.dumps(item.snapshot)
        worker.execute_item(db, item)
    assert [row['key'] for row in writes] == ['test-key-one', 'test-key-two']
    assert [row['base_url'] for row in writes] == urls and [row['other'] for row in writes] == versions
    assert all(row['type'] == (3 if kind == 'azure_gpt' else 14) for row in writes)
    assert len(records) == 2


def test_old_and_new_gpt_templates_coexist_without_legacy_upload_entry(db, login, catalog_setup):
    root, client = login('root'), login('user')
    category, old_fmt, old_template = setup_azure(db, root, catalog_setup, legacy=True)
    second = Site(id=uid(), name='Second Azure', prefix='az2', base_url='https://azure-second.invalid',
        seller_user_id='1', token_encrypted=encrypt('fixture-token'), adapter=catalog_setup.adapter,
        enabled=True, health='healthy', verified_at=catalog_setup.verified_at, capabilities=catalog_setup.capabilities)
    db.add(second)
    db.commit()
    _, new_fmt, _ = setup_azure(db, root, second)
    option = next(row for row in client.get('/api/uploads/options').json()['items'] if row['category_id'] == category.id)
    assert option['ready'] and option['format_id'] == new_fmt.id and option['target_count'] == 2
    assert old_fmt.id not in json.dumps(option['formats'])
    denied = client.post('/api/uploads/simple-preview', json={'category_id': category.id,
        'format_id': old_fmt.id, 'credentials': 'legacy-plain-key'})
    assert not denied.json()['can_submit']
    changed = root.patch('/api/upload-templates/' + old_template.id, json={'remark': 'Keep old format', 'channel_config': {'status': 2}})
    assert changed.status_code == 200 and changed.json()['format_id'] == old_fmt.id
    accepted = client.post('/api/uploads/simple-submit', json={'category_id': category.id,
        'credentials': 'resource-new|new-key|2024-10-21', 'idempotency_key': 'azure-old-and-new'})
    assert accepted.status_code == 200 and accepted.json()['total'] == 2, accepted.text


def test_rotation_keeps_resource_and_version_and_sends_only_replacement_key(db, login, catalog_setup, monkeypatch):
    category, fmt, _ = setup_azure(db, login('root'), catalog_setup)
    client = login('user')
    response = client.post('/api/uploads/simple-submit', json={'category_id': category.id, 'format_id': fmt.id,
        'credentials': 'resource-one|original-key|2024-10-21', 'idempotency_key': 'azure-rotation-fixture'})
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    records, writes = attach(monkeypatch)
    worker.execute_item(db, item)
    item.status, db.get(Task, item.task_id).status = 'succeeded', 'succeeded'
    db.commit()
    for value in ('other-resource|replacement-key|2024-10-21', 'resource-one|replacement-key|2025-01-01-preview'):
        assert client.post('/api/channels/' + item.channel_id + '/rotate', json={'key': value}).status_code == 422
    changed = client.post('/api/channels/' + item.channel_id + '/rotate', json={'key': 'resource-one|replacement-key|2024-10-21'})
    assert changed.status_code == 200, changed.text
    rotated = db.scalar(select(TaskItem).where(TaskItem.task_id == changed.json()['task_id']))
    worker.execute_item(db, rotated)
    assert writes[-1] == {'key': 'replacement-key'}
    assert next(iter(records.values()))['base_url'] == 'https://resource-one.openai.azure.com'


def test_tampered_endpoint_snapshot_stops_before_write(db, login, catalog_setup, monkeypatch):
    category, fmt, _ = setup_azure(db, login('root'), catalog_setup)
    response = login('user').post('/api/uploads/simple-submit', json={'category_id': category.id, 'format_id': fmt.id,
        'credentials': 'resource-one|original-key|2024-10-21', 'idempotency_key': 'azure-tamper-fixture'})
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    item.snapshot = {**item.snapshot, 'channel_config': {**item.snapshot['channel_config'], 'base_url': 'https://attacker.invalid'}}
    db.commit()
    _, writes = attach(monkeypatch)
    with pytest.raises(worker.WriteStopped, match='Azure'):
        worker.execute_item(db, item)
    assert writes == [] and db.get(Distribution, item.distribution_id).remote_id is None
