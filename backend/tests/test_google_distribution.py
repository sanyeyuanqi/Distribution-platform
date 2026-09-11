"""Service isolation and per-credential Vertex authentication without live calls."""
# ruff: noqa: F811
import json
from types import SimpleNamespace

import pytest
from app import worker
from app.adapters.channel_config import create_payload, validate_readback
from app.bootstrap import seed_catalog
from app.channel_service import parse_rows, refresh_task
from app.db import uid
from app.google_services import CLAUDE9
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate, User
from app.models_channels import Channel, Distribution, KeyVersion, Task, TaskItem
from app.newapi_formats import (
    credential_fingerprint,
    credential_wire_schema,
    normalize_credential,
)
from app.security import decrypt, encrypt, fingerprint
from sqlalchemy import func, select
from test_catalog_policy import catalog_setup  # noqa: F401

GEMINI = ['gemini-2.5-flash', 'gemini-2.5-pro']
ACCOUNT = {'type': 'service_account', 'project_id': 'fixture-project',
           'client_email': 'fixture@fixture-project.iam.gserviceaccount.com', 'private_key': 'fixture-private-key'}


def definition(kind):
    return SimpleNamespace(schema_config={'type': kind, 'remote_type': 41})


def google_catalog(db, site):
    site.capabilities = {**site.capabilities, 'custom_models': True}
    db.commit()
    category = db.scalar(select(Category).where(Category.family == 'Google'))
    formats = {row.schema_config['type']: row for row in db.scalars(select(CredentialFormat).where(
        CredentialFormat.category_id == category.id))}
    return category, formats


def template(db, root, site, category, fmt, *, models=None, enabled=True):
    response = root.post('/api/upload-templates', json={'site_id': site.id, 'category_id': category.id,
        'format_id': fmt.id, 'models': models if models is not None else (
            list(CLAUDE9) if fmt.schema_config['type'] == 'vertex_claude' else GEMINI),
        'routing_group': 'default', 'enabled': enabled})
    assert response.status_code == 201, response.text
    return db.get(SiteUploadTemplate, response.json()['id'])


def submit(client, category, fmt, *, credentials=None, models=None, nonce='google-fixture-0001'):
    response = client.post('/api/uploads/simple-submit', json={'category_id': category.id, 'format_id': fmt.id,
        'credentials': credentials if credentials is not None else json.dumps(ACCOUNT),
        'models': models if models is not None else (list(CLAUDE9) if fmt.schema_config['type'] == 'vertex_claude' else GEMINI),
        'idempotency_key': nonce})
    assert response.status_code == 200, response.text
    return response.json()


def attach(monkeypatch):
    records, writes = {}, []
    class FakeAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return next((row for row in records.values() if row['name'] == name), None)

        def create(self, **values):
            self.before_write()
            payload = create_payload(**values, supported_types={24, 41})
            remote_id = str(len(records) + 1)
            records[remote_id] = {'id': int(remote_id), **payload}
            writes.append(payload)
            return remote_id

        def detail(self, remote_id):
            return records[str(remote_id)]

        def validate_created_config(self, remote, *, config, proxy, channel_type):
            validate_readback(remote, config, proxy, channel_type, supported_types={24, 41})

        def edit(self, remote_id, *, changes, key=None, expected=None):
            current = records[str(remote_id)]
            assert not expected or all(current.get(k) == v for k, v in expected.items())
            self.before_write()
            current.update(changes)
            if key:
                current['key'] = key
            writes.append({'operation': 'edit', 'key': key})
    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write: FakeAdapter(site, before_write))
    return records, writes


def execute(db, result):
    for row in result['items']:
        item = db.get(TaskItem, row['id'])
        worker.execute_item(db, item)
        item.status, item.stage = 'succeeded', 'complete'
        db.flush()
        refresh_task(db, db.get(Task, item.task_id))
        db.commit()


def test_vertex_mixed_records_dedupe_and_choose_authentication_per_record():
    key = 'fixture-vertex-api-key'
    credentials = json.dumps([ACCOUNT, key, ACCOUNT])
    rows, errors = parse_rows(credentials, fmt=definition('vertex_gemini'))
    assert not errors and [row['status'] for row in rows] == ['valid', 'valid', 'duplicate']
    assert [credential_wire_schema(row['key'], definition('vertex_gemini').schema_config)['type']
            for row in rows[:2]] == ['vertex_json', 'vertex_api_key']
    assert rows[0]['key_hint'].startswith('JSON ')
    assert rows[0]['fingerprint'] != fingerprint(rows[0]['key'])
    assert credential_fingerprint(rows[0]['key'], definition('vertex_claude')) != rows[0]['fingerprint']
    assert credential_fingerprint(rows[0]['key'], definition('vertex_json')) == fingerprint(rows[0]['key'])
    jsonl, errors = parse_rows(json.dumps(ACCOUNT) + '\n' + key, fmt=definition('vertex_gemini'))
    assert not errors and [row['status'] for row in jsonl] == ['valid', 'valid']


@pytest.mark.parametrize('value', ['[]', '{"private_key":"secret-missing-fields"}',
                                 json.dumps({**ACCOUNT, 'type': 'external_account'})])
def test_vertex_claude_rejects_non_service_account_credentials_without_echo(value):
    with pytest.raises(ValueError) as error:
        normalize_credential(value, definition('vertex_claude'))
    assert 'secret-missing-fields' not in str(error.value)


def test_three_services_are_independent_and_same_account_can_serve_both_vertex_services(db, login, catalog_setup, monkeypatch):
    root, client = login('root'), login('user')
    category, fmts = google_catalog(db, catalog_setup)
    templates = {kind: template(db, root, catalog_setup, category, fmt) for kind, fmt in fmts.items()}
    option = next(row for row in client.get('/api/uploads/options').json()['items'] if row['category_id'] == category.id)
    assert [row['variant'] for row in option['variants']] == ['ai_studio_gemini', 'vertex_gemini', 'vertex_claude']
    assert all(row['ready'] and row['target_count'] == 1 and len(row['formats']) == 1 for row in option['variants'])
    assert option['variants'][2]['models'] == list(CLAUDE9)
    assert all(row['models'] == GEMINI for row in option['variants'][:2])
    assert option['variants'][1]['formats'][0]['input_mode'] == 'auto'
    records, writes = attach(monkeypatch)
    studio = submit(client, category, fmts['api_key'], credentials='studio-key', nonce='studio-fixture-001')
    gemini = submit(client, category, fmts['vertex_gemini'], credentials=json.dumps([ACCOUNT, 'vertex-key']), nonce='gemini-fixture-001')
    selected = [CLAUDE9[0], CLAUDE9[4]]
    claude = submit(client, category, fmts['vertex_claude'], models=selected, nonce='claude-fixture-001')
    for result, kind in ((studio, 'api_key'), (gemini, 'vertex_gemini'), (claude, 'vertex_claude')):
        for row in result['items']:
            item = db.get(TaskItem, row['id'])
            assert item.snapshot['upload_template_id'] == templates[kind].id
            assert 'fixture-private-key' not in json.dumps(item.snapshot)
        execute(db, result)
    assert len(records) == 4
    assert [row['type'] for row in writes] == [24, 41, 41, 41]
    assert [json.loads(row['settings']).get('vertex_key_type') for row in writes[1:]] == ['json', 'api_key', 'json']
    assert all(row['base_url'] is None and row['other'] == '{"default":"global"}' for row in writes[1:])
    assert writes[-1]['models'] == ','.join(selected)
    assert json.loads(writes[-1]['model_mapping']) == {CLAUDE9[0]: 'claude-haiku-4-5@20251001'}
    channels = list(db.scalars(select(Channel)))
    shared = [channel for channel in channels if decrypt(channel.key_encrypted).startswith('{')]
    assert len(shared) == 2 and shared[0].fingerprint != shared[1].fingerprint
    repeat = client.post('/api/uploads/simple-preview', json={'category_id': category.id,
        'format_id': fmts['vertex_gemini'].id, 'credentials': json.dumps(ACCOUNT), 'models': GEMINI})
    assert repeat.json()['rows'][0]['status'] == 'already_distributed'
    assert db.scalar(select(func.count()).select_from(Channel)) == 3


@pytest.mark.parametrize('kind', ['api_key', 'vertex_gemini', 'vertex_claude'])
def test_service_model_scopes_reject_cross_service_before_intersection(db, login, catalog_setup, kind):
    category, fmts = google_catalog(db, catalog_setup)
    template(db, login('root'), catalog_setup, category, fmts[kind])
    models = [CLAUDE9[0], GEMINI[0]]
    response = login('user').post('/api/uploads/simple-submit', json={'category_id': category.id,
        'format_id': fmts[kind].id, 'credentials': json.dumps(ACCOUNT) if kind != 'api_key' else 'studio-key',
        'models': models, 'idempotency_key': 'cross-service-models'})
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Channel)) == 0
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_disabled_vertex_claude_does_not_borrow_gemini_target_or_models(db, login, catalog_setup):
    category, fmts = google_catalog(db, catalog_setup)
    root = login('root')
    template(db, root, catalog_setup, category, fmts['vertex_gemini'])
    template(db, root, catalog_setup, category, fmts['vertex_claude'], enabled=False)
    option = next(row for row in login('user').get('/api/uploads/options').json()['items'] if row['category_id'] == category.id)
    variants = {row['variant']: row for row in option['variants']}
    assert variants['vertex_gemini']['ready']
    assert not variants['vertex_claude']['ready'] and variants['vertex_claude']['models'] == []
    assert variants['vertex_claude']['configured_models'] == list(CLAUDE9)


def test_vertex_wire_snapshot_tampering_stops_before_remote_create(db, login, catalog_setup, monkeypatch):
    category, fmts = google_catalog(db, catalog_setup)
    template(db, login('root'), catalog_setup, category, fmts['vertex_claude'])
    result = submit(login('user'), category, fmts['vertex_claude'])
    records, writes = attach(monkeypatch)
    item = db.get(TaskItem, result['items'][0]['id'])
    item.snapshot = {**item.snapshot, 'wire_format_schema': {'type': 'vertex_api_key', 'remote_type': 41}}
    db.commit()
    with pytest.raises(worker.WriteStopped, match='认证方式快照'):
        worker.assert_execution(db, db.get(Task, item.task_id), item, write=True)
    assert not records and not writes


def test_vertex_rotation_keeps_authentication_and_scoped_identity(db, login, catalog_setup, monkeypatch):
    category, fmts = google_catalog(db, catalog_setup)
    template(db, login('root'), catalog_setup, category, fmts['vertex_gemini'])
    client = login('user')
    result = submit(client, category, fmts['vertex_gemini'])
    records, writes = attach(monkeypatch)
    execute(db, result)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    assert client.post('/api/channels/' + channel.id + '/rotate', json={'key': 'vertex-api-key'}).status_code == 422
    replacement = json.dumps({**ACCOUNT, 'private_key': 'replacement-private-key'})
    response = client.post('/api/channels/' + channel.id + '/rotate', json={'key': replacement})
    assert response.status_code == 200, response.text
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == response.json()['task_id']))
    records['1']['settings'] = '{"vertex_key_type":"api_key"}'
    with pytest.raises(Exception, match='认证方式已改变'):
        worker.execute_item(db, item)
    assert len(writes) == 1  # Only initial create; no rotation PUT.
    db.refresh(channel)
    assert channel.fingerprint == credential_fingerprint(normalize_credential(replacement, fmts['vertex_gemini']), fmts['vertex_gemini'])
    assert len(list(db.scalars(select(KeyVersion).where(KeyVersion.channel_id == channel.id)))) == 2


def test_rotation_allows_other_service_key_but_rejects_same_service_duplicate(db, login, catalog_setup, monkeypatch):
    category, fmts = google_catalog(db, catalog_setup)
    root, client = login('root'), login('user')
    for kind in ('vertex_gemini', 'vertex_claude'):
        template(db, root, catalog_setup, category, fmts[kind])
    other_key = json.dumps({**ACCOUNT, 'private_key': 'other-service-key'})
    third_key = json.dumps({**ACCOUNT, 'private_key': 'same-service-key'})
    first = submit(client, category, fmts['vertex_gemini'], nonce='rotation-first-fixture')
    other = submit(client, category, fmts['vertex_claude'], credentials=other_key, nonce='rotation-other-fixture')
    third = submit(client, category, fmts['vertex_gemini'], credentials=third_key, nonce='rotation-third-fixture')
    _records, writes = attach(monkeypatch)
    for result in (first, other, third):
        execute(db, result)
    channel = db.get(Channel, first['items'][0]['channel_id'])
    other_channel = db.get(Channel, other['items'][0]['channel_id'])
    rejected = client.post('/api/channels/' + channel.id + '/rotate', json={'key': third_key})
    assert rejected.status_code == 409
    db.refresh(channel)
    assert channel.key_version == 1
    accepted = client.post('/api/channels/' + channel.id + '/rotate', json={'key': other_key})
    assert accepted.status_code == 200, accepted.text
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == accepted.json()['task_id']))
    worker.execute_item(db, item)
    db.refresh(channel)
    db.refresh(other_channel)
    assert decrypt(channel.key_encrypted) == decrypt(other_channel.key_encrypted)
    assert channel.fingerprint != other_channel.fingerprint and other_channel.key_version == 1
    assert len(writes) == 4


def test_adopted_channel_model_edit_and_forged_worker_edit_cannot_cross_service(db, login, catalog_setup, monkeypatch):
    from app.routers.channels import existing_operation
    category, fmts = google_catalog(db, catalog_setup)
    template(db, login('root'), catalog_setup, category, fmts['vertex_gemini'])
    client = login('user')
    result = submit(client, category, fmts['vertex_gemini'])
    _records, writes = attach(monkeypatch)
    execute(db, result)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    channel.upload_mode = 'advanced'  # Same editable mode used by historical adoption.
    db.commit()
    response = client.patch('/api/channels/' + channel.id, json={'models': ['claude-opus-4-6']})
    assert response.status_code == 422
    assert channel.models == GEMINI
    actor = db.get(User, db.get(Task, result['id']).actor_id)
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id)))
    task = existing_operation(db, actor, channel, 'edit', distributions, changes={'models': 'claude-opus-4-6'})
    db.commit()
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == task.id))
    with pytest.raises(worker.WriteStopped, match='Gemini 不能接收 Claude'):
        worker.assert_execution(db, task, item, write=True)
    assert len(writes) == 1


def test_bootstrap_migrates_studio_variant_without_rewriting_receiving_fields_or_historical_data(db, login, catalog_setup):
    category, fmts = google_catalog(db, catalog_setup)
    row = template(db, login('root'), catalog_setup, category, fmts['api_key'])
    row.variant = ''
    row.name, row.remark = 'Existing custom name', 'Keep this note'
    db.commit()
    before = {name: getattr(row, name) for name in ('id', 'format_id', 'name', 'remark', 'models', 'routing_group', 'enabled')}
    version = row.version
    seed_catalog(db)
    db.commit()
    assert row.variant == 'ai_studio_gemini' and row.version == version + 1
    assert {name: getattr(row, name) for name in before} == before
    seed_catalog(db)
    db.commit()
    assert row.version == version + 1


def test_legacy_vertex_json_stays_private_and_can_redistribute_with_original_identity(db, login, catalog_setup, monkeypatch):
    root, client = login('root'), login('user')
    category, fmts = google_catalog(db, catalog_setup)
    legacy = CredentialFormat(id=uid(), category_id=category.id, code='newapi-41-vertex-json-v1', name='Old JSON',
        version='1', enabled=True, schema_config={'type': 'vertex_json', 'remote_type': 41})
    db.add(legacy)
    db.flush()
    first = SiteUploadTemplate(id=uid(), site_id=catalog_setup.id, category_id=category.id, format_id=legacy.id,
        name='Old Gemini', variant='vertex_gemini', enabled=True, models=GEMINI, routing_group='default', channel_config={'status': 2})
    db.add(first)
    db.commit()
    _records, writes = attach(monkeypatch)
    result = submit(client, category, fmts['vertex_gemini'])
    execute(db, result)
    channel = db.get(Channel, result['items'][0]['channel_id'])
    # Model an immutable channel created before the business formats existed.
    channel.format_id, channel.fingerprint = legacy.id, fingerprint(decrypt(channel.key_encrypted))
    db.commit()
    before_key, before_identity = channel.key_encrypted, channel.fingerprint
    second = Site(id=uid(), name='Second Google', prefix='google2', base_url='https://google-second.invalid',
        seller_user_id='1', token_encrypted=encrypt('fixture-token'), adapter=catalog_setup.adapter,
        enabled=True, health='healthy', verified_at=catalog_setup.verified_at, capabilities=catalog_setup.capabilities)
    db.add(second)
    db.commit()
    template(db, root, second, category, fmts['vertex_gemini'])
    denied = client.post('/api/uploads/simple-preview', json={'category_id': category.id, 'format_id': legacy.id,
        'credentials': json.dumps(ACCOUNT), 'models': GEMINI})
    assert not denied.json()['can_submit']
    response = client.post('/api/channels/' + channel.id + '/actions', json={'action': 'redistribute', 'site_ids': [second.id]})
    assert response.status_code == 200, response.text
    task = db.get(Task, response.json()['id'])
    item = db.scalar(select(TaskItem).where(TaskItem.task_id == task.id))
    assert item.snapshot['format_code'] == legacy.code and item.snapshot['format_schema'] == legacy.schema_config
    worker.execute_item(db, item)
    assert db.scalar(select(func.count()).select_from(Channel)) == 1
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2
    db.refresh(channel)
    assert channel.format_id == legacy.id and channel.key_encrypted == before_key and channel.fingerprint == before_identity
    assert len(writes) == 2
