"""Fixed catalog, legacy-data preservation and server-side upload gates."""
import json

import pytest
from app import worker
from app.bootstrap import initialize, seed_catalog, seed_newapi_formats
from app.catalog_policy import (
    CATALOG_FAMILIES,
    catalog_category_filter,
    get_catalog_format_specs,
)
from app.channel_service import channel_snapshot, supported_format
from app.db import uid, utcnow
from app.models import AuditEvent, Category, CredentialFormat, Site, SiteUploadTemplate
from app.models_channels import Channel, Distribution, Task, TaskItem, UploadGroup
from app.newapi_formats import get_format_specs
from app.security import encrypt, fingerprint
from app.upload_templates import effective_template
from sqlalchemy import func, select


@pytest.fixture
def catalog_setup(db, users, monkeypatch):
    for module in ('uploads', 'tasks', 'channels'):
        monkeypatch.setattr(f'app.routers.{module}.notify_worker', lambda: None)

    def no_network(*args, **kwargs):
        pytest.fail('Catalog policy tests must not contact an upstream')

    monkeypatch.setattr('app.adapters.silicon.safe_request', no_network)
    initialize(db)
    site = Site(id=uid(), name='Catalog fixture', prefix='catalog', base_url='https://catalog.invalid',
        seller_user_id='1', token_encrypted=encrypt('catalog-fixture-token'), adapter='new-api-v1',
        enabled=True, health='healthy', verified_at=utcnow(), capabilities={
            'can_write': True, 'can_edit_routing': True, 'create': 'supported',
            'formats': [spec['code'] for spec in get_format_specs()], 'groups': ['default'],
            'models': ['fixture-model'], 'channel_config': 'supported', 'proxy': 'supported'})
    db.add(site)
    db.commit()
    return site


def add_legacy(db, site, *, name='DeepSeek', family='DeepSeek', code_type=43):
    category = Category(id=uid(), name=name, family=family, active=True)
    db.add(category)
    db.flush()
    spec = next(spec for spec in get_format_specs() if spec['remote_type'] == code_type)
    fmt = CredentialFormat(id=uid(), category_id=category.id, code=spec['code'], version=spec['version'],
        name=spec['name'], schema_config=spec['schema_config'], enabled=True, default_models=['fixture-model'])
    db.add(fmt)
    db.flush()
    template = SiteUploadTemplate(id=uid(), site_id=site.id, category_id=category.id, format_id=fmt.id,
        models=['fixture-model'], routing_group='default', name='Historical template', enabled=True)
    db.add(template)
    db.commit()
    return category, fmt, template


def template_body(category, fmt, site, **overrides):
    return {'category_id': category.id, 'format_id': fmt.id, 'site_id': site.id,
            'models': ['fixture-model'], 'routing_group': 'default', **overrides}


def test_initialize_reinstalls_fixed_definitions_without_reinterpreting_old_versions(db, users):
    initialize(db)
    rows = list(db.scalars(select(Category)))
    assert {row.name for row in rows} == set(CATALOG_FAMILIES)
    assert len(rows) == 7 and all(row.name == row.family for row in rows)
    original = {row.id for row in db.scalars(select(CredentialFormat))}
    openai = next(row for row in rows if row.family == 'OpenAI')
    openai.name, openai.active = 'Previously editable name', False
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == openai.id))
    fmt.name, fmt.version = 'Previously editable format', '7'
    fmt.schema_config = {'type': 'untrusted', 'remote_type': 57}
    fmt.enabled, fmt.default_models = False, ['obsolete-format-default']
    db.commit()
    initialize(db)
    assert len(list(db.scalars(select(Category)))) == 7
    restored = list(db.scalars(select(CredentialFormat)))
    assert original < {row.id for row in restored}
    db.refresh(openai)
    db.refresh(fmt)
    assert openai.active and openai.name == 'Previously editable name'
    assert not fmt.enabled and fmt.default_models == ['obsolete-format-default'] and fmt.name == 'Previously editable format'
    assert fmt.version == '7' and fmt.schema_config == {'type': 'untrusted', 'remote_type': 57}
    assert {spec['remote_type'] for spec in get_format_specs()} >= {1, 3, 14, 20, 24, 33, 41, 43, 57, 59, 60}
    assert {spec['family'] for spec in get_catalog_format_specs()} == set(CATALOG_FAMILIES)
    assert len(get_catalog_format_specs()) == 13
    canonical = next(row for row in restored if row.category_id == openai.id and row.version == '1')
    assert canonical.schema_config == {'type': 'api_key', 'remote_type': 1}
    assert canonical.enabled and canonical.default_models == []
    before = {row.id for row in restored}
    initialize(db)
    assert {row.id for row in db.scalars(select(CredentialFormat))} == before


def test_bootstrap_preserves_extra_catalog_rows_and_templates_without_moving_history(db, users, catalog_setup, login):
    legacy, fmt, template = add_legacy(db, catalog_setup)
    group = UploadGroup(id=uid(), owner_id=users['user'].id, category_id=legacy.id, format_id=fmt.id,
                        tag='legacy-catalog-group')
    db.add(group)
    db.flush()
    channel = Channel(id=uid(), owner_id=users['user'].id, group_id=group.id, category_id=legacy.id,
        format_id=fmt.id, key_encrypted=encrypt('historical-fixture-key'), key_hint='••••',
        fingerprint=fingerprint('historical-fixture-key'), models=['fixture-model'])
    db.add(channel)
    db.commit()
    old_ids = (legacy.id, fmt.id, template.id, channel.id)
    initialize(db)
    db.expire_all()
    assert legacy.active and template.enabled
    assert fmt.enabled and fmt.category_id == legacy.id and channel.category_id == legacy.id
    assert (legacy.id, fmt.id, template.id, channel.id) == old_ids
    assert template.models == ['fixture-model'] and channel.format_id == fmt.id
    client = login('user')
    detail = client.get('/api/channels/' + channel.id)
    assert detail.status_code == 200 and detail.json()['category_name'] == 'DeepSeek'
    assert client.get('/api/dashboard').json()['categories'][0]['category_name'] == 'DeepSeek'
    assert db.scalar(select(func.count()).select_from(Category)) == 8
    assert len(list(db.scalars(select(Category).where(catalog_category_filter())))) == 7


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_catalog_api_has_canonical_order_and_filters_legacy_formats_before_restart(db, login, catalog_setup, role):
    extra, fmt, _ = add_legacy(db, catalog_setup)
    client = login(role)
    listing = client.get('/api/categories')
    assert listing.status_code == 200
    assert [row['name'] for row in listing.json()['items']] == list(CATALOG_FAMILIES)
    assert listing.json()['total'] == 7
    assert extra.id not in listing.text and fmt.id not in client.get('/api/formats').text
    assert client.get('/api/formats', params={'category_id': extra.id}).json() == {'items': [], 'total': 0}
    if role != 'root':
        options = client.get('/api/uploads/options').json()
        assert [row['family'] for row in options['items']] == list(CATALOG_FAMILIES)
        assert extra.id not in json.dumps(options)


def test_catalog_rejects_eighth_category_rename_family_and_hidden_format_mutations(db, login, catalog_setup):
    root = login('root')
    openai = db.scalar(select(Category).where(Category.name == 'OpenAI'))
    extra, fmt, _ = add_legacy(db, catalog_setup)
    for body in ({'name': 'Custom OpenAI', 'family': 'OpenAI'}, {'name': 'OpenAI', 'family': 'OpenAI'},
                 {'name': 'DeepSeek new', 'family': 'DeepSeek'}):
        assert root.post('/api/categories', json=body).status_code == 405
    for category_id, body in ((openai.id, {'name': 'Renamed'}), (openai.id, {'family': 'AWS'}),
                              (extra.id, {'active': True}), (openai.id, {'active': False})):
        assert root.patch('/api/categories/' + category_id, json=body).status_code in (404, 405)
    assert root.get('/api/categories').json()['total'] == 7
    assert root.patch('/api/categories/' + openai.id, json={'active': True}).status_code in (404, 405)
    assert root.patch('/api/formats/' + fmt.id, json={'enabled': True}).status_code in (404, 405)
    assert root.post('/api/formats', json={'category_id': extra.id, 'code': 'draft', 'name': 'Draft',
        'version': '1', 'enabled': False, 'schema_config': {'type': 'api_key', 'remote_type': 43}}).status_code == 405
    db.refresh(openai)
    assert openai.name == 'OpenAI' and openai.active


@pytest.mark.parametrize(('name', 'family', 'code_type'), [('DeepSeek', 'DeepSeek', 43), ('Legacy Vertex', 'VertexAI', 41)])
def test_hidden_ids_cannot_create_templates_or_upload_even_when_active(db, login, catalog_setup, name, family, code_type):
    extra, fmt, template = add_legacy(db, catalog_setup, name=name, family=family, code_type=code_type)
    root, client = login('root'), login('user')
    assert not supported_format(extra, fmt)
    assert template.id not in root.get('/api/upload-templates').text
    assert root.get('/api/upload-templates', params={'category_id': extra.id}).json()['items'] == []
    for enabled in (False, True):
        assert root.patch('/api/upload-templates/' + template.id, json={'enabled': enabled}).status_code == 422
        assert root.post('/api/upload-templates', json=template_body(extra, fmt, catalog_setup, enabled=enabled)).status_code == 422
    simple = {'category_id': extra.id, 'format_id': fmt.id, 'credentials': 'new-fixture-key'}
    preview = client.post('/api/uploads/simple-preview', json=simple)
    assert preview.status_code == 200 and not preview.json()['can_submit']
    assert client.post('/api/uploads/simple-submit', json={**simple, 'idempotency_key': 'hidden-simple-key'}).status_code == 422
    advanced = {**simple, 'models': ['fixture-model']}
    assert not client.post('/api/uploads/preview', json=advanced).json()['can_submit']
    assert client.post('/api/uploads/submit', json={**advanced, 'idempotency_key': 'hidden-advanced-key'}).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_google_vertex_formats_are_separate_and_opencode_pending_definition_is_preserved(db, users, catalog_setup, login):
    legacy, legacy_fmt, legacy_template = add_legacy(db, catalog_setup, name='VertexAI', family='VertexAI', code_type=41)
    opencode = db.scalar(select(Category).where(Category.name == 'OpenCode'))
    opencode_fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == opencode.id))
    # A previous unsupported definition remains untouched instead of changing meaning.
    opencode_fmt.code, opencode_fmt.enabled = 'pending', False
    opencode_fmt.schema_config = {'status': 'pending'}
    old_id = opencode_fmt.id
    db.commit()
    seed_catalog(db)
    seed_newapi_formats(db)
    db.commit()
    db.expire_all()
    google = db.scalar(select(Category).where(Category.name == 'Google'))
    formats = list(db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == google.id)))
    assert sorted(fmt.schema_config['remote_type'] for fmt in formats) == [24, 41, 41]
    assert legacy_fmt.id not in {fmt.id for fmt in formats}
    assert legacy_fmt.category_id == legacy.id and legacy_template.category_id == legacy.id
    assert legacy.active and legacy_template.enabled
    assert all(supported_format(google, fmt) for fmt in formats)
    assert opencode_fmt.id == old_id and opencode_fmt.code == 'pending'
    assert opencode_fmt.schema_config == {'status': 'pending'}
    assert not supported_format(opencode, opencode_fmt)
    assert db.scalar(select(func.count()).select_from(CredentialFormat).where(CredentialFormat.category_id == opencode.id)) == 2
    listed = login('root').get('/api/categories').json()['items']
    assert next(row for row in listed if row['family'] == 'OpenCode')['default_base_url'] == 'https://opencode.ai/zen'


@pytest.mark.parametrize('config', [None, {'base_url': '', 'priority': 4, 'model_mapping': {'fixture-model': 'upstream-model'}}])
def test_opencode_official_defaults_are_derived_without_template_fields(db, login, catalog_setup, config):
    category = db.scalar(select(Category).where(Category.name == 'OpenCode'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    root = login('root')
    body = template_body(category, fmt, catalog_setup, enabled=True)
    if config is not None:
        body['channel_config'] = config
    created = root.post('/api/upload-templates', json=body)
    assert created.status_code == 201 and created.json()['ready'], created.text
    saved = db.get(SiteUploadTemplate, created.json()['id'])
    assert saved.channel_config == {'status': 2}
    effective = effective_template(saved, category=category, fmt=fmt)['channel_config']
    assert effective['base_url'] == 'https://opencode.ai/zen'
    assert effective['priority'] == 0 and effective['model_mapping'] == {}


def test_opencode_legacy_default_is_only_applied_on_save_and_bumps_version(db, login, catalog_setup):
    category = db.scalar(select(Category).where(Category.name == 'OpenCode'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    row = SiteUploadTemplate(id=uid(), **template_body(category, fmt, catalog_setup, enabled=False),
        channel_config={'priority': 7}, version=8)
    db.add(row)
    db.commit()
    root = login('root')
    listed = root.get('/api/upload-templates').json()['items'][0]
    assert listed['channel_config'] == {'status': 2}
    assert listed['version'] == 8
    db.refresh(row)
    assert row.channel_config == {'priority': 7}
    saved = root.patch('/api/upload-templates/' + row.id, json={})
    assert saved.status_code == 200 and saved.json()['version'] == 9
    db.refresh(row)
    assert row.channel_config == {'status': 2}
    assert not row.enabled
    event = db.scalar(select(AuditEvent).where(AuditEvent.object_id == row.id))
    assert event.summary['fields'] == ['channel_config']
    assert root.patch('/api/upload-templates/' + row.id, json={}).json()['version'] == 9


def test_opencode_custom_address_cannot_override_official_endpoint(db, login, catalog_setup):
    category = db.scalar(select(Category).where(Category.name == 'OpenCode'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    root = login('root')
    created = root.post('/api/upload-templates', json=template_body(category, fmt, catalog_setup,
        channel_config={'base_url': 'https://custom-opencode.example/zen', 'weight': 3}))
    assert created.status_code == 201
    saved = root.patch('/api/upload-templates/' + created.json()['id'], json={'name': 'Custom endpoint'})
    assert saved.status_code == 200
    assert saved.json()['channel_config'] == {'status': 2}
    row = db.get(SiteUploadTemplate, saved.json()['id'])
    effective = effective_template(row, category=category, fmt=fmt)['channel_config']
    assert effective['base_url'] == 'https://opencode.ai/zen' and effective['weight'] == 1


@pytest.mark.parametrize('adapter', ['silicon-v1', 'tcp-red-v1', 'new-api-v1'])
@pytest.mark.parametrize(('family', 'format_code', 'overrides'), [
    ('Azure', 'newapi-3-azure-gpt-v1', {'base_url': 'https://unrelated-provider.invalid'}),
    ('Google', 'newapi-41-vertex-gemini-v1', {'other': '{"default":"us-central1"}'}),
    ('Google', 'newapi-41-vertex-claude-v1', {'other': '{"default":"us-central1","model-a":42}'}),
])
def test_provider_overrides_are_discarded_for_every_template_adapter(
        db, login, catalog_setup, adapter, family, format_code, overrides):
    catalog_setup.adapter = adapter
    db.commit()
    category = db.scalar(select(Category).where(Category.family == family))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id,
        CredentialFormat.code == format_code))
    root = login('root')
    body = template_body(category, fmt, catalog_setup, channel_config=overrides, enabled=False)
    draft = root.post('/api/upload-templates', json=body)
    assert draft.status_code == 201
    row = db.get(SiteUploadTemplate, draft.json()['id'])
    assert not row.enabled and row.channel_config == {'status': 2}
    saved = root.patch('/api/upload-templates/' + row.id, json={'channel_config': overrides})
    assert saved.status_code == 200 and saved.json()['channel_config'] == {'status': 2}, saved.text
    effective = effective_template(row, category=category, fmt=fmt,
        credential='catalog-resource|fixture-key|2025-04-01-preview' if family == 'Azure' else None)['channel_config']
    if family == 'Azure':
        assert effective['base_url'] == 'https://catalog-resource.openai.azure.com'
        assert effective['other'] == '2025-04-01-preview'
    else:
        assert json.loads(effective['other']) == {'default': 'global'}


@pytest.mark.parametrize(('config', 'message'), [
    ({'auto_ban': 0}, '此站点版本不支持自动禁用设置'),
    ({'status_code_mapping': {'429': '503'}}, '此站点版本不支持状态码映射'),
])
def test_template_overrides_cannot_enable_verified_unsupported_fields(
        db, login, catalog_setup, config, message):
    catalog_setup.adapter = 'silicon-v1'
    catalog_setup.capabilities = {**catalog_setup.capabilities,
        'unsupported_config_fields': ['auto_ban', 'status_code_mapping']}
    db.commit()
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    root = login('root')
    body = template_body(category, fmt, catalog_setup)
    created = root.post('/api/upload-templates', json={**body, 'channel_config': config})
    assert created.status_code == 201 and created.json()['ready'], created.text
    path = '/api/upload-templates/' + created.json()['id']
    defaults = root.patch(path, json={'channel_config': {'auto_ban': 1, 'status_code_mapping': {}}})
    assert defaults.status_code == 200 and defaults.json()['ready'], defaults.text
    overridden = root.patch(path, json={'channel_config': config})
    assert overridden.status_code == 200 and overridden.json()['channel_config'] == {'status': 2}
    row = db.get(SiteUploadTemplate, created.json()['id'])
    effective = effective_template(row, category=category, fmt=fmt)['channel_config']
    assert effective['auto_ban'] == 1 and effective['status_code_mapping'] == {}
    # Even a newer site cannot restore hidden template overrides through this API.
    db.refresh(catalog_setup)
    catalog_setup.capabilities = {key: value for key, value in catalog_setup.capabilities.items()
        if key != 'unsupported_config_fields'}
    db.commit()
    allowed = root.patch(path, json={'channel_config': config})
    assert allowed.status_code == 200 and allowed.json()['ready'], allowed.text
    assert allowed.json()['channel_config'] == {'status': 2}


def test_worker_refuses_hidden_category_but_readonly_reconciliation_keeps_history(db, users, login, catalog_setup, monkeypatch):
    category, fmt, _ = add_legacy(db, catalog_setup)
    group = UploadGroup(id=uid(), owner_id=users['user'].id, category_id=category.id, format_id=fmt.id, tag='legacy-worker')
    db.add(group)
    db.flush()
    channel = Channel(id=uid(), owner_id=users['user'].id, group_id=group.id, category_id=category.id, format_id=fmt.id,
        key_encrypted=encrypt('legacy-worker-key'), key_hint='••••', fingerprint=fingerprint('legacy-worker-key'), models=['fixture-model'])
    db.add(channel)
    db.flush()
    distribution = Distribution(id=uid(), channel_id=channel.id, site_id=catalog_setup.id,
        remote_name='legacy-stable-name', models=['fixture-model'])
    task = Task(id=uid(), actor_id=users['user'].id, owner_id=users['user'].id,
                actor_session_version=users['user'].session_version, kind='upload')
    db.add_all([distribution, task])
    db.flush()
    item = TaskItem(id=uid(), task_id=task.id, channel_id=channel.id, distribution_id=distribution.id,
        site_id=catalog_setup.id, operation='create', key_version=channel.key_version,
        snapshot=channel_snapshot(channel, fmt, catalog_setup))
    db.add(item)
    db.commit()
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(worker.WriteStopped, match='格式'):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted
    item.stage, item.remote_write_attempted = 'create_sent', True
    category.active = False
    db.commit()

    class ReadOnly:
        def find_unique_name(self, name):
            return {'id': 301, 'name': name, 'type': 43, 'status': 2, 'models': 'fixture-model', 'group': 'default'}

    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: ReadOnly())
    worker.execute_item(db, item)
    assert distribution.remote_id == '301'


def test_catalog_exposes_authenticated_reads_but_no_write_routes(db, login, client, catalog_setup):
    assert client.get('/api/categories').status_code == 401
    assert client.get('/api/formats').status_code == 401
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    for role in ('root', 'admin', 'user'):
        authenticated = login(role)
        categories = authenticated.get('/api/categories')
        formats = authenticated.get('/api/formats')
        assert categories.status_code == formats.status_code == 200
        assert categories.json()['total'] == 7 and formats.json()['total'] == 13
        assert authenticated.post('/api/categories', json={'name': 'No new category'}).status_code == 405
        assert authenticated.patch('/api/categories/' + category.id, json={'active': True}).status_code in (404, 405)
        assert authenticated.patch('/api/formats/' + fmt.id, json={'enabled': True}).status_code in (404, 405)
        assert authenticated.post('/api/formats', json={'category_id': category.id, 'code': 'custom', 'name': 'Custom',
            'schema_config': {'type': 'api_key', 'remote_type': 1}}).status_code == 405
        for path in ('/api/categories/' + category.id, '/api/formats/' + fmt.id):
            for method in ('PUT', 'DELETE'):
                assert authenticated.request(method, path, json={}).status_code in (404, 405)
    db.expire_all()
    assert category.active and fmt.enabled


def test_bootstrap_preserves_receiving_scopes_while_simplifying_fourteen_site_templates(db, users, catalog_setup):
    second_site = Site(id=uid(), name='Second fixture', prefix='second', base_url='https://second-catalog.invalid',
        seller_user_id='1', token_encrypted=encrypt('second-fixture-token'), adapter='new-api-v1')
    db.add(second_site)
    db.flush()
    categories = list(db.scalars(select(Category).where(catalog_category_filter())))
    definitions = list(db.scalars(select(CredentialFormat)))
    original_ids = {row.id for row in definitions}
    fields = ('site_id', 'category_id', 'format_id', 'name', 'enabled', 'models', 'routing_group', 'remark')
    for category in categories:
        fmt = next(row for row in definitions if row.category_id == category.id)
        for site in (catalog_setup, second_site):
            db.add(SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id,
                name='Keep ' + category.family, models=['keep-' + category.family], routing_group='custom-route',
                enabled=False, remark='Keep template note', version=8,
                channel_config={'status': 1, 'priority': 7}, proxy_encrypted=encrypt('http://fixture:secret@proxy.invalid:81')))
    db.commit()
    before = {row.id: {field: getattr(row, field) for field in fields} for row in db.scalars(select(SiteUploadTemplate))}
    assert len(before) == 14
    for category in categories:
        category.name, category.active = 'Edited ' + category.family, False
    for fmt in definitions:
        fmt.name, fmt.enabled, fmt.version = 'Edited format', False, 'legacy-9'
        fmt.default_models = ['obsolete-default']
    db.flush()
    openai = next(row for row in categories if row.family == 'OpenAI')
    duplicate = CredentialFormat(category_id=openai.id, code='api_key-v1', name='Extra old version',
        version='legacy-2', schema_config={'type': 'api_key', 'remote_type': 1}, enabled=True)
    db.add(duplicate)
    db.commit()
    initialize(db)
    db.expire_all()
    after = {row.id: {field: getattr(row, field) for field in fields} for row in db.scalars(select(SiteUploadTemplate))}
    assert after == before
    assert all(category.name == 'Edited ' + category.family and category.active for category in categories)
    for fmt in definitions:
        assert fmt.id in original_ids and not fmt.enabled and fmt.version == 'legacy-9' and fmt.default_models == ['obsolete-default']
        assert 'type' in fmt.schema_config
    assert duplicate.enabled and duplicate.version == 'legacy-2'
    assert original_ids | {duplicate.id} < {row.id for row in db.scalars(select(CredentialFormat))}
    migrated = list(db.scalars(select(SiteUploadTemplate)))
    assert all(row.channel_config == {'status': 1} and row.proxy_encrypted is None and row.version == 9 for row in migrated)
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == 'upload_template.fixed_defaults')) == 14
    initialize(db)
    db.expire_all()
    assert all(row.version == 9 for row in migrated)
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == 'upload_template.fixed_defaults')) == 14


def test_advanced_upload_ignores_editable_defaults_and_rejects_wrong_family_protocol(db, login, catalog_setup):
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    canonical = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    canonical.default_models = ['fixture-model']
    db.commit()
    root, client = login('root'), login('user')
    advanced = {'category_id': category.id, 'format_id': canonical.id, 'credentials': 'model-selection-fixture'}
    defaults = client.post('/api/uploads/preview', json=advanced).json()
    assert not defaults['can_submit']
    assert client.post('/api/uploads/preview', json={**advanced, 'models': ['fixture-model']}).json()['can_submit']
    assert root.get('/api/formats', params={'category_id': category.id}).json()['items'][0]['default_models'] == []
    spec = next(spec for spec in get_format_specs() if spec['remote_type'] == 57)
    forged = CredentialFormat(id=uid(), category_id=category.id, code=spec['code'], name='Injected wire format',
        version='1', schema_config=spec['schema_config'], enabled=True)
    db.add(forged)
    db.commit()
    assert not supported_format(category, forged)
    assert forged.id not in root.get('/api/formats').text
    assert root.post('/api/upload-templates', json=template_body(category, forged, catalog_setup, enabled=False)).status_code == 422
    db.add(SiteUploadTemplate(site_id=catalog_setup.id, category_id=category.id, format_id=forged.id,
        enabled=True, models=['fixture-model'], routing_group='default'))
    db.commit()
    simple = {'category_id': category.id, 'format_id': forged.id,
              'credentials': json.dumps({'access_token': 'fixture-only', 'account_id': 'fixture-account'})}
    assert not client.post('/api/uploads/simple-preview', json=simple).json()['can_submit']
    assert client.post('/api/uploads/simple-submit', json={**simple, 'idempotency_key': 'invalid-fixed-format'}).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_bootstrap_restores_fixed_metadata_but_rejects_protocol_collisions(db, users):
    initialize(db)
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    original_id = fmt.id
    fmt.name, fmt.enabled, fmt.default_models = 'Editable name', False, ['old-model']
    fmt.schema_config = {'type': 'api_key', 'remote_type': 1, 'help': 'Custom instructions'}
    db.commit()
    initialize(db)
    db.refresh(fmt)
    assert fmt.id == original_id and fmt.enabled and fmt.default_models == []
    assert fmt.schema_config == {'type': 'api_key', 'remote_type': 1}
    assert fmt.name == next(spec['name'] for spec in get_catalog_format_specs() if spec['code'] == fmt.code)
    fmt.schema_config = {'type': 'codex_json', 'remote_type': 57}
    db.commit()
    with pytest.raises(RuntimeError, match='历史解析规则冲突'):
        initialize(db)
    db.rollback()
    db.refresh(fmt)
    assert fmt.id == original_id and fmt.schema_config == {'type': 'codex_json', 'remote_type': 57}


def test_bootstrap_migrates_legacy_azure_template_variant_without_rebinding_keys(db, users, catalog_setup):
    category = db.scalar(select(Category).where(Category.family == 'Azure'))
    spec = next(spec for spec in get_format_specs() if spec['code'] == 'newapi-3-api-key-v1')
    fmt = CredentialFormat(id=uid(), category_id=category.id, code=spec['code'], name=spec['name'],
        version='1', schema_config=spec['schema_config'], enabled=True)
    db.add(fmt)
    db.flush()
    group = UploadGroup(id=uid(), owner_id=users['user'].id, category_id=category.id, format_id=fmt.id, tag='legacy-azure')
    db.add(group)
    db.flush()
    channel = Channel(id=uid(), owner_id=users['user'].id, group_id=group.id, category_id=category.id,
        format_id=fmt.id, key_encrypted=encrypt('bare-azure-fixture-key'), key_hint='••••',
        fingerprint=fingerprint('bare-azure-fixture-key'), models=['fixture-model'])
    row = SiteUploadTemplate(id=uid(), **template_body(category, fmt, catalog_setup), enabled=False,
        channel_config={'status': 2, 'base_url': 'https://old-resource.openai.azure.com', 'other': '2025-04-01-preview'},
        variant='', version=4)
    db.add_all([channel, row])
    db.commit()
    ciphertext = channel.key_encrypted
    initialize(db)
    db.expire_all()
    assert row.variant == 'azure_gpt' and row.version == 5 and row.channel_config == {'status': 2}
    assert row.format_id == channel.format_id == fmt.id and channel.key_encrypted == ciphertext
    assert fmt.schema_config == {'type': 'api_key', 'remote_type': 3}
    assert row.models == ['fixture-model'] and row.routing_group == 'default'
    initialize(db)
    db.refresh(row)
    assert row.version == 5
