"""Template upload acceptance: DB ledgers and stubbed adapters, never remote writes."""
import json
import re
from datetime import UTC, datetime

import pytest
from app import worker
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.models_channels import Channel, Distribution, Task, TaskItem, UploadGroup
from app.newapi_formats import get_format_specs
from app.security import decrypt, encrypt, fingerprint
from app.upload_templates import SimpleSubmitInput, batch_group_tag, resolve_templates
from sqlalchemy import func, select


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Template tests must not access external services')
    monkeypatch.setattr('app.adapters.silicon.safe_request', forbidden)
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.channels.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: None)


@pytest.fixture
def catalog(db, users):
    cat = Category(id=uid(), name='OpenAI', family='OpenAI', active=True)
    db.add(cat)
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=cat.id, code='api_key-v1', name='API Key', version='1',
                           enabled=True, schema_config={'type': 'api_key', 'remote_type': 1}, default_models=[])
    db.add(fmt)
    sites = []
    for i in range(3):
        site = Site(id=uid(), name=f'Site {i}', prefix=f'S{i}', base_url=f'https://private-{i}.example', seller_user_id='33',
                    adapter='silicon-v1', token_encrypted=encrypt('private-seller-token'), token_hint='private-hint',
                    enabled=True, health='healthy', verified_at=utcnow(), capabilities={'models': ['model-a', 'model-b'],
                        'groups': ['default', 'route-a', 'route-b'], 'formats': ['api_key-v1'], 'can_write': True,
                        'create': 'supported', 'remark_max_length': 255})
        db.add(site)
        sites.append(site)
    db.commit()
    return cat, fmt, sites


def template_body(catalog, index=0, **values):
    cat, fmt, sites = catalog
    return {'site_id': sites[index].id, 'category_id': cat.id, 'format_id': fmt.id,
            'models': ['model-a' if index == 0 else 'model-b'], 'routing_group': 'route-a' if index == 0 else 'route-b',
            'remark': f'default note {index}', 'enabled': True, **values}


def add_templates(client, catalog, count=2):
    result = []
    for i in range(count):
        response = client.post('/api/upload-templates', json=template_body(catalog, i))
        assert response.status_code == 201, response.text
        result.append(response.json())
    return result


def upload(catalog, **values):
    return {'category_id': catalog[0].id, 'credentials': 'test-credential-alpha', **values}


def submit(client, catalog, **values):
    response = client.post('/api/uploads/simple-submit', json=upload(catalog, **{'idempotency_key': 'template-test-0001', **values}))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_only_superadmin_can_manage_templates(login, catalog, role):
    client = login(role)
    assert client.get('/api/upload-templates').status_code == 403
    assert client.post('/api/upload-templates', json=template_body(catalog)).status_code == 403
    assert client.patch('/api/upload-templates/missing', json={'enabled': False}).status_code == 403
    assert client.delete('/api/upload-templates/missing').status_code == 403


def test_superadmin_cannot_simple_upload(login, catalog):
    root = login('root')
    assert root.get('/api/uploads/options').status_code == 403
    assert root.post('/api/uploads/simple-preview', json=upload(catalog)).status_code == 403
    assert root.post('/api/uploads/simple-submit', json=upload(catalog, idempotency_key='root-no-upload')).status_code == 403


def test_template_crud_drafts_version_and_unique(login, catalog):
    root = login('root')
    draft = root.post('/api/upload-templates', json=template_body(catalog, enabled=False, models=[])).json()
    assert not draft['ready'] and draft['version'] == 1
    assert root.patch('/api/upload-templates/' + draft['id'], json={'enabled': True}).status_code == 422
    saved = root.patch('/api/upload-templates/' + draft['id'], json={'models': ['model-a'], 'enabled': True}).json()
    assert saved['enabled'] and not saved['ready'] and saved['version'] == 2
    assert saved['issues'] == ['站点已停用或归档']  # Repairing a draft does not restart the site's distribution switch.
    assert root.patch('/api/upload-templates/' + draft['id'], json={'enabled': True}).json()['version'] == 2
    assert root.post('/api/upload-templates', json=template_body(catalog)).status_code == 409
    assert root.get('/api/upload-templates', params={'site_id': catalog[2][0].id}).json()['total'] == 1
    assert root.delete('/api/upload-templates/' + draft['id']).json()['deleted']
    assert root.get('/api/upload-templates').json()['total'] == 0


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_delete_permission_keeps_existing_template(db, login, catalog, role):
    saved = add_templates(login('root'), catalog, count=1)[0]
    response = login(role).delete('/api/upload-templates/' + saved['id'])
    assert response.status_code == 403
    db.expire_all()
    assert db.get(SiteUploadTemplate, saved['id']) is not None
    assert db.get(Site, saved['site_id']).enabled


def test_delete_template_preserves_channels_settlements_and_task_history(db, login, catalog, users, monkeypatch):
    from copy import deepcopy

    from app.models import AuditEvent
    from settlement_fixtures import order_for
    from sqlalchemy import inspect

    root = login('root')
    saved = add_templates(root, catalog, count=1)[0]
    client = login('user')
    completed = submit(client, catalog)
    completed_item = db.get(TaskItem, completed['items'][0]['id'])
    completed_task = db.get(Task, completed_item.task_id)
    completed_item.status = completed_task.status = 'succeeded'
    dist = db.get(Distribution, completed_item.distribution_id)
    dist.remote_id, dist.status = '77', 'disabled'
    channel = db.get(Channel, completed_item.channel_id)
    order, line = order_for(db, users, channel)
    db.commit()
    pending = submit(client, catalog, credentials='another-test-credential', idempotency_key='pending-delete-template')
    pending_item = db.get(TaskItem, pending['items'][0]['id'])
    records = [channel, dist, completed_task, completed_item, order, line,
               db.get(Channel, pending_item.channel_id), db.get(Distribution, pending_item.distribution_id),
               db.get(Task, pending_item.task_id), pending_item]

    def persisted_values(row):
        return deepcopy({column.key: getattr(row, column.key) for column in inspect(type(row)).columns})

    before = [persisted_values(row) for row in records]
    response = root.delete('/api/upload-templates/' + saved['id'])
    assert response.status_code == 200, response.text
    assert response.json() == {'deleted': True, 'id': saved['id']}
    db.expire_all()
    assert db.get(SiteUploadTemplate, saved['id']) is None
    assert [persisted_values(row) for row in records] == before
    assert not db.get(Site, saved['site_id']).enabled
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'upload_template.delete')))
    assert len(events) == 1 and events[0].object_id == saved['id']
    assert root.delete('/api/upload-templates/' + saved['id']).status_code == 404

    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(worker.WriteStopped, match='模板'):
        worker.execute_item(db, pending_item)
    assert not pending_item.remote_write_attempted


@pytest.mark.parametrize('bad', [{'models': []}, {'models': ['unknown']}, {'routing_group': 'other'},
                                 {'routing_group': 'route-a,other'}, {'remark': 'x' * 256}])
def test_enabled_templates_validate_remote_capabilities(login, catalog, bad):
    assert login('root').post('/api/upload-templates', json=template_body(catalog, **bad)).status_code == 422


@pytest.mark.parametrize('failed', [False, True])
def test_unknown_site_capabilities_do_not_claim_models_groups_or_formats_unsupported(db, login, catalog, failed):
    root = login('root')
    saved = add_templates(root, catalog, count=1)[0]
    site = catalog[2][0]
    site.verified_at = None
    site.health = 'healthy'  # Historical health values cannot imply verification.
    site.capabilities = ({'verification_error': {'category': 'protocol_error', 'message': 'obsolete message'}}
                         if failed else {})
    db.commit()
    result = root.get('/api/upload-templates').json()['items'][0]
    assert result['site_verification_status'] == ('failed' if failed else 'unverified')
    assert result['ready'] is False and len(result['issues']) == 1
    if failed:
        assert result['issues'][0] == '站点验证失败：' + result['site_verification_error']['message']
    else:
        assert result['site_verification_error'] is None
        assert result['issues'] == ['站点尚未验证，请先验证站点']
    assert root.patch('/api/upload-templates/' + saved['id'], json={'enabled': True}).status_code == 422
    response = login('user').post('/api/uploads/simple-submit', json=upload(catalog, idempotency_key='unverified-site'))
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_failed_site_keeps_local_template_and_availability_issues(db, login, catalog):
    site = catalog[2][0]
    site.capabilities = {'verification_error': {'category': 'permission_denied'}}
    site.verified_at = None
    site.enabled = False
    db.commit()
    response = login('root').post('/api/upload-templates', json=template_body(catalog, enabled=False, models=[]))
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['ready'] is False
    assert set(result['issues']) == {'模板已停用', '模板尚未配置模型', '站点已停用或归档',
                                      '站点验证失败：卖家账号没有所需接口权限'}


def test_verified_site_retains_real_unsupported_capability_errors(db, login, catalog):
    site = catalog[2][0]
    site.capabilities = {**site.capabilities, 'can_write': False, 'create': 'permission_denied',
                         'formats': [], 'models': [], 'groups': []}
    db.commit()
    response = login('root').post('/api/upload-templates', json=template_body(catalog, enabled=False))
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['site_verification_status'] == 'verified' and result['site_verification_error'] is None
    assert result['ready'] is False
    assert '站点没有创建渠道权限' in result['issues']
    assert '站点不支持所选凭据格式' in result['issues']
    assert '不支持的模型：model-a' in result['issues']
    assert any('站点不允许路由组 route-a' in issue for issue in result['issues'])


def test_multiple_groups_normalize_roundtrip_and_allow_draft_unavailable_groups(db, login, catalog):
    root = login('root')
    response = root.post('/api/upload-templates', json=template_body(catalog,
        routing_group=' route-b , route-a, route-b '))
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved['routing_group'] == 'route-b,route-a' and saved['ready']
    assert db.get(SiteUploadTemplate, saved['id']).routing_group == 'route-b,route-a'
    listed = root.get('/api/upload-templates').json()['items'][0]
    assert listed['routing_group'] == saved['routing_group']
    unchanged = root.patch('/api/upload-templates/' + saved['id'], json={
        'routing_group': 'route-b, route-a,route-b'})
    assert unchanged.json()['version'] == saved['version']
    changed = root.patch('/api/upload-templates/' + saved['id'], json={
        'routing_group': 'route-a,unavailable', 'enabled': False})
    assert changed.status_code == 200 and not changed.json()['ready']
    assert changed.json()['routing_group'] == 'route-a,unavailable'
    rejected = root.patch('/api/upload-templates/' + saved['id'], json={'enabled': True})
    assert rejected.status_code == 422 and 'unavailable' in rejected.text


@pytest.mark.parametrize('groups', ['', ' ', ',route-a', 'route-a,', 'route-a,,route-b',
    'route-a\n', 'route-a\r', 'route-a\t', 'route-a\x00', 'x' * 161,
    ','.join(f'g{i}' for i in range(33)), 'route-a,' * 600])
def test_template_group_selection_rejects_empty_segments_controls_and_limits(groups):
    from app.routers.upload_templates import TemplateCreate, TemplatePatch
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TemplateCreate(site_id='site', category_id='category', format_id='format', routing_group=groups)
    with pytest.raises(ValidationError):
        TemplatePatch(routing_group=groups)


def test_multiple_groups_are_frozen_and_delivered_as_one_remote_channel(db, login, catalog, monkeypatch):
    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog,
        routing_group='route-a,route-b')).json()
    result = submit(login('user'), catalog)
    assert len(result['items']) == 1
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.snapshot['routing_group'] == 'route-a,route-b'
    assert item.snapshot['template_config']['routing_group'] == 'route-a,route-b'
    assert item.snapshot['upload_template_id'] == template['id']
    sent = []

    class MockAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            sent.append(values)
            return '88'

        def detail(self, remote_id):
            return {'id': 88, 'name': sent[0]['name'], 'type': 1, 'status': 2,
                    'models': ','.join(sent[0]['models']), 'group': sent[0]['group']}

    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    worker.execute_item(db, item)
    assert len(sent) == 1 and sent[0]['group'] == 'route-a,route-b'
    dist = db.get(Distribution, item.distribution_id)
    assert dist.routing_group == 'route-a,route-b' and dist.remote_id == '88'


@pytest.mark.parametrize('change', ['template_group', 'revoked_group'])
def test_multiple_group_queued_write_stops_when_selection_or_permissions_change(db, login, catalog, monkeypatch, change):
    from app.adapters.silicon import RemoteError

    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog,
        routing_group='route-a,route-b')).json()
    result = submit(login('user'), catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    if change == 'template_group':
        assert root.patch('/api/upload-templates/' + template['id'], json={'routing_group': 'route-a'}).status_code == 200
        expected_error, message = worker.WriteStopped, '模板'
    else:
        site = db.get(Site, template['site_id'])
        site.capabilities = {**site.capabilities, 'groups': ['default', 'route-a']}
        db.commit()
        expected_error, message = RemoteError, 'route-b'
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(expected_error, match=message):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted
    assert item.snapshot['routing_group'] == 'route-a,route-b'


def test_options_are_safe_and_no_untemplated_site_fallback(db, login, catalog):
    templates = add_templates(login('root'), catalog)
    disabled = db.get(Site, catalog[2][1].id)
    disabled.enabled = False
    db.commit()
    client = login('user')
    result = client.get('/api/uploads/options').json()
    item = result['items'][0]
    assert item['target_count'] == 1 and item['ready']
    assert item['targets'][0]['id'] == templates[0]['site_id']
    assert item['skipped_targets'][0]['id'] == disabled.id
    text = json.dumps(result)
    assert item['models'] == ['model-a']
    for secret in ('private-', 'private-hint', 'token_encrypted', 'seller_user_id', 'route-a', 'default note'):
        assert secret not in text
    assert catalog[2][2].id not in text


@pytest.mark.parametrize('disabled', ['template', 'site'])
def test_configured_models_preview_survives_disabled_template_or_site_without_granting_upload(db, login, catalog, disabled):
    saved = add_templates(login('root'), catalog, count=1)[0]
    if disabled == 'template':
        db.get(SiteUploadTemplate, saved['id']).enabled = False
    else:
        db.get(Site, saved['site_id']).enabled = False
    db.commit()
    client = login('user')
    option = client.get('/api/uploads/options').json()['items'][0]
    assert option['configured_models'] == ['model-a']
    assert option['models'] == [] and option['target_count'] == 0 and not option['ready']
    assert option['targets'] == []
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, models=['model-a'])).json()
    assert not preview['can_submit'] and any('模型列表' in error for error in preview['errors'])
    assert client.post('/api/uploads/simple-submit', json=upload(catalog,
        models=['model-a'], idempotency_key='configured-models-cannot-upload')).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_configured_model_union_does_not_expand_live_override_allowlist(db, login, catalog):
    saved = add_templates(login('root'), catalog)
    disabled = db.get(SiteUploadTemplate, saved[1]['id'])
    disabled.enabled = False
    disabled.models = ['model-b', 'model-a']
    db.commit()
    client = login('admin')
    option = client.get('/api/uploads/options').json()['items'][0]
    assert option['configured_models'] == ['model-a', 'model-b']
    assert option['models'] == ['model-a'] and option['ready'] and option['target_count'] == 1
    selected = client.post('/api/uploads/simple-preview', json=upload(catalog, models=['model-b'])).json()
    assert not selected['can_submit'] and any('模型列表' in error for error in selected['errors'])
    accepted = client.post('/api/uploads/simple-preview', json=upload(catalog, models=['model-a'])).json()
    assert accepted['can_submit']
    assert all(isinstance(name, str) for name in option['configured_models'])
    assert not any(secret in json.dumps(option['configured_models']) for secret in (
        'private-', 'private-hint', 'route-a', 'default note', saved[0]['id'], saved[0]['site_id']))


def test_explicit_nine_model_override_excludes_other_template_aliases_from_distribution(db, login, catalog):
    selected = ['claude-haiku-4-5-20251001', 'claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8',
                'claude-sonnet-5', 'claude-fable-5', 'claude-opus-5', 'claude-sonnet-4-6', 'claude-fable-5-1']
    models = [*selected, *(f'claude-other-fixture-{index}' for index in range(34))]
    category, fmt, sites = catalog
    category.name = category.family = 'Anthropic'
    fmt.code, fmt.schema_config = 'newapi-14-api-key-v1', {'type': 'api_key', 'remote_type': 14}
    sites[0].capabilities = {**sites[0].capabilities, 'formats': [fmt.code], 'custom_models': True}
    db.commit()
    saved = login('root').post('/api/upload-templates', json=template_body(catalog, models=models))
    assert saved.status_code == 201, saved.text
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, models=selected)).json()
    assert preview['can_submit'] and preview['configured_models'] == models
    result = submit(client, catalog, models=selected)
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.snapshot['models'] == selected
    assert db.get(Distribution, item.distribution_id).models == selected
    assert db.get(Channel, item.channel_id).upload_settings['models'] == selected
    assert db.get(SiteUploadTemplate, saved.json()['id']).models == models


@pytest.mark.parametrize('excluded', ['archived_site', 'unsupported_adapter', 'disabled_format',
    'unsupported_format', 'disabled_category', 'missing_format', 'malformed_models', 'invalid_model_name'])
def test_configured_model_preview_excludes_archived_unsupported_or_malformed_sources(db, login, catalog, excluded):
    saved = add_templates(login('root'), catalog, count=1)[0]
    template = db.get(SiteUploadTemplate, saved['id'])
    template.enabled = False
    site = db.get(Site, saved['site_id'])
    fmt = db.get(CredentialFormat, saved['format_id'])
    if excluded == 'archived_site':
        site.archived = True
    elif excluded == 'unsupported_adapter':
        site.adapter = 'unsupported-adapter'
    elif excluded == 'disabled_format':
        fmt.enabled = False
    elif excluded == 'unsupported_format':
        fmt.schema_config = {'type': 'unreviewed', 'remote_type': 1}
    elif excluded == 'disabled_category':
        db.get(Category, saved['category_id']).active = False
    elif excluded == 'missing_format':
        other_category = Category(id=uid(), name='Anthropic', family='Anthropic', active=True)
        db.add(other_category)
        db.flush()
        fmt.category_id = other_category.id
    elif excluded == 'malformed_models':
        template.models = {'not': 'a model list'}
    else:
        template.models = ['model-a', 'invalid,model']
    db.commit()
    public, targets, _, _ = resolve_templates(db, saved['category_id'])
    assert public['configured_models'] == [] and public['models'] == []
    assert not public['ready'] and not targets


def enable_extended_contract(db, catalog):
    for site in catalog[2]:
        site.capabilities = {**site.capabilities, 'channel_config': 'supported', 'proxy': 'supported', 'can_toggle': True}
    db.commit()


def test_template_keeps_status_and_discards_removed_settings_without_secret_echo(db, login, catalog):
    enable_extended_contract(db, catalog)
    root = login('root')
    proxy = 'socks5://proxy-user:private-proxy-password@proxy.example:1080'
    response = root.post('/api/upload-templates', json=template_body(catalog,
        channel_config={'status': 1, 'priority': 9, 'weight': 3, 'base_url': 'https://upstream.example/v1',
                        'account_info': {'balance_usd': 0, 'prepaid': False}}, proxy=proxy))
    assert response.status_code == 201, response.text
    row = db.get(SiteUploadTemplate, response.json()['id'])
    assert row.proxy_encrypted is None and row.channel_config == {'status': 1}
    body = response.json()
    assert 'has_proxy' not in body and 'proxy_hint' not in body
    assert proxy not in response.text and 'private-proxy-password' not in response.text
    assert 'proxy_encrypted' not in response.text and 'proxy-user' not in response.text
    same = root.patch('/api/upload-templates/' + row.id, json={'proxy': proxy})
    assert same.json()['version'] == body['version']
    cleared = root.patch('/api/upload-templates/' + row.id, json={'proxy': ''})
    assert 'has_proxy' not in cleared.json() and cleared.json()['version'] == body['version']
    db.refresh(row)
    assert row.proxy_encrypted is None


@pytest.mark.parametrize('config', [
    {'status': 3}, {'weight': -1}, {'priority': True}, {'account_info': {'rpm': -1}},
    {'account_info': {'balance_usd': -0.1}}, {'account_info': {'prepaid': 'false'}},
    {'rpm_enabled': True}, {'status_code_mapping': {'999': '200'}},
    {'base_url': 'https://secret:password@example.com'}, {'unknown_secret': 'must-not-store'},
])
def test_template_fixed_settings_reject_invalid_or_unknown_fields(login, catalog, config):
    response = login('root').post('/api/upload-templates', json=template_body(catalog,
        enabled=False, channel_config=config))
    assert response.status_code == 422
    assert 'must-not-store' not in response.text


def test_model_subset_skips_unmatched_sites_and_rejects_empty_intersection(db, login, catalog):
    add_templates(login('root'), catalog)
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, models=['model-b'])).json()
    assert preview['can_submit'] and preview['target_count'] == 1
    assert preview['targets'][0]['id'] == catalog[2][1].id
    assert '模型' in preview['skipped_targets'][0]['reason']
    result = submit(client, catalog, models=['model-b'])
    assert result['total'] == 1
    channel = db.get(Channel, result['items'][0]['channel_id'])
    assert channel.models == ['model-b'] and channel.upload_settings == {'models': ['model-b']}
    bad = client.post('/api/uploads/simple-preview', json=upload(catalog, models=['unknown'])).json()
    assert not bad['can_submit'] and any('模型列表' in value for value in bad['errors'])


def test_each_site_receives_only_intersection_even_when_key_models_include_unreceived_names(db, login, catalog, monkeypatch):
    root = login('root')
    receivers = [['shared', 'model-a'], ['model-b', 'shared'], ['model-c']]
    for site in catalog[2]:
        site.capabilities = {**site.capabilities, 'models': ['shared', 'model-a', 'model-b', 'model-c']}
    db.commit()
    for index, models in enumerate(receivers):
        response = root.post('/api/upload-templates', json=template_body(catalog, index=index, models=models))
        assert response.status_code == 201, response.text
    scope = ['model-b', 'key-only-model', 'shared']
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, models=scope)).json()
    assert preview['can_submit'] and preview['target_count'] == 2
    assert preview['skipped_targets'][0]['id'] == catalog[2][2].id
    result = submit(client, catalog, models=scope)
    assert result['total'] == 2 and db.scalar(select(func.count()).select_from(Distribution)) == 2
    channel = db.get(Channel, result['items'][0]['channel_id'])
    assert channel.upload_settings['models'] == scope
    assert channel.models == ['shared', 'model-b']
    expected = {catalog[2][0].id: ['shared'], catalog[2][1].id: ['model-b', 'shared']}
    sent = {}

    class IntersectionAdapter:
        def __init__(self, site, before_write):
            self.site, self.before_write = site, before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            sent[self.site.id] = values
            return '88'

        def detail(self, remote_id):
            values = sent[self.site.id]
            return {'id': 88, 'name': values['name'], 'type': 1, 'status': 2,
                    'models': ','.join(values['models']), 'group': values['group']}

    monkeypatch.setattr(worker, 'get_adapter', IntersectionAdapter)
    for row in result['items']:
        item = db.get(TaskItem, row['id'])
        assert item.snapshot['models'] == expected[item.site_id]
        worker.execute_item(db, item)
        assert sent[item.site_id]['models'] == expected[item.site_id]
        assert db.get(Distribution, item.distribution_id).models == expected[item.site_id]
    assert catalog[2][2].id not in sent


def test_empty_intersection_creates_no_channel_distribution_or_task(db, login, catalog):
    add_templates(login('root'), catalog)
    response = login('user').post('/api/uploads/simple-submit', json=upload(catalog,
        models=['key-only-model'], idempotency_key='no-model-intersection'))
    assert response.status_code == 422 and '没有交集' in response.text
    for model in (Channel, Distribution, Task, TaskItem, UploadGroup):
        assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('entrypoint', ['action', 'simple_submit'])
def test_supplemental_distribution_uses_original_key_scope_and_skips_empty_sites(db, login, catalog, entrypoint):
    root = login('root')
    root.post('/api/upload-templates', json=template_body(catalog))
    scope = ['model-a', 'model-b', 'key-only-model']
    client = login('user')
    original = submit(client, catalog, models=scope)
    channel_id = original['items'][0]['channel_id']
    for item in db.scalars(select(TaskItem)):
        item.status = 'succeeded'
    site = catalog[2][2]
    site.capabilities = {**site.capabilities, 'models': ['model-c']}
    db.commit()
    root.post('/api/upload-templates', json=template_body(catalog, index=1))
    root.post('/api/upload-templates', json=template_body(catalog, index=2, models=['model-c']))
    if entrypoint == 'action':
        response = client.post('/api/channels/' + channel_id + '/actions', json={'action': 'redistribute'})
        assert response.status_code == 200, response.text
        result = response.json()
    else:
        result = submit(client, catalog, models=scope, idempotency_key='supplemental-model-intersection')
    assert result['total'] == 1
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.site_id == catalog[2][1].id and item.channel_id == channel_id
    assert item.snapshot['models'] == ['model-b']
    assert db.get(Distribution, item.distribution_id).models == ['model-b']
    assert db.get(Channel, channel_id).upload_settings['models'] == scope
    assert db.scalar(select(func.count()).select_from(Channel)) == 1
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


@pytest.mark.parametrize('recovery', ['retry', 'reprepare', 'reprepare_empty'])
def test_recovery_preserves_key_scope_and_rebuild_uses_current_receiver_intersection(db, login, catalog, recovery):
    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog, models=['model-a', 'model-b'])).json()
    client = login('user')
    scope = ['model-b', 'key-only-model']
    original = submit(client, catalog, models=scope)
    task = db.get(Task, original['id'])
    item = db.get(TaskItem, original['items'][0]['id'])
    task.status = item.status = 'failed'
    db.commit()
    if recovery == 'retry':
        response = client.post('/api/tasks/' + task.id + '/retry')
    else:
        receiving = ['model-a'] if recovery == 'reprepare_empty' else ['model-b']
        assert root.patch('/api/upload-templates/' + template['id'], json={'models': receiving}).status_code == 200
        response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    if recovery == 'reprepare_empty':
        assert response.status_code == 422
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(func.count()).select_from(TaskItem)) == 1
    else:
        assert response.status_code == 200, response.text
        fresh = db.get(TaskItem, response.json()['items'][0]['id'], populate_existing=True)
        assert fresh.snapshot['models'] == ['model-b']
        assert fresh.snapshot['upload_settings_snapshot']['models'] == scope
        assert db.get(Distribution, fresh.distribution_id).models == ['model-b']
    assert db.get(Channel, item.channel_id).upload_settings['models'] == scope


@pytest.mark.parametrize('frozen_models', [[], ['model-a', 'model-b'], ['key-only-model']])
def test_worker_rejects_frozen_models_outside_template_key_intersection(db, login, catalog, monkeypatch, frozen_models):
    login('root').post('/api/upload-templates', json=template_body(catalog, models=['model-a', 'model-b']))
    original = submit(login('user'), catalog, models=['model-b', 'key-only-model'])
    item = db.get(TaskItem, original['items'][0]['id'])
    item.snapshot = {**item.snapshot, 'models': frozen_models}
    db.commit()
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(worker.WriteStopped, match='交集'):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted


def test_intersection_rejects_malformed_stored_key_scope_instead_of_substring_matching():
    from app.upload_templates import intersect_template_models

    with pytest.raises(ValueError, match='模型范围'):
        intersect_template_models(['model-a'], 'prefix-model-a-suffix')
    assert intersect_template_models(['model-a', 'model-b'], []) == []
    assert intersect_template_models(['model-a', 'model-b']) == ['model-a', 'model-b']


def configure_claude_mapping_catalog(db, catalog, family):
    models = ['claude-haiku-4-5-20251001', 'claude-opus-4-6', 'claude-sonnet-4-6']
    category, fmt, sites = catalog
    category.name = category.family = family
    remote_type = 20 if family == 'OpenRouter' else 14
    fmt.code, fmt.schema_config = f'newapi-{remote_type}-api-key-v1', {'type': 'api_key', 'remote_type': remote_type}
    for site in sites:
        site.capabilities = {**site.capabilities, 'formats': [fmt.code], 'models': models,
                             'channel_types': [remote_type], 'channel_config': 'supported', 'can_edit_routing': True}
    db.commit()
    return models


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode'])
def test_fixed_provider_mapping_is_derived_after_intersection_and_frozen_without_template_mutation(db, login, catalog, monkeypatch, family):
    from app.adapters.channel_config import create_payload, validate_readback
    from app.upload_templates import effective_template

    receiving = configure_claude_mapping_catalog(db, catalog, family)
    haiku, opus, _ = receiving
    expected = {haiku: 'anthropic/claude-haiku-4.5', opus: 'anthropic/claude-opus-4.6'} if family == 'OpenRouter' else {
        haiku: 'claude-haiku-4-5'}
    root = login('root')
    response = root.post('/api/upload-templates', json=template_body(catalog, models=receiving))
    assert response.status_code == 201, response.text
    template = db.get(SiteUploadTemplate, response.json()['id'])
    assert response.json()['channel_config'] == {'status': 2}
    raw_config = dict(template.channel_config)
    effective = effective_template(template, category=catalog[0])
    assert effective['config_explicit']
    assert effective['channel_config']['model_mapping'].get(haiku) == expected[haiku]
    client = login('user')
    result = submit(client, catalog, models=[haiku, opus, 'key-only-model'])
    item = db.get(TaskItem, result['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    assert item.snapshot['models'] == [haiku, opus]
    assert item.snapshot['channel_config']['model_mapping'] == expected
    assert dist.template_snapshot['effective_channel_config']['model_mapping'] == expected
    assert item.snapshot['template_config']['channel_config'] == raw_config
    assert template.channel_config == raw_config
    sent = []
    options = {'supported_types': {14, 20}, 'routing': True, 'supports_proxy': False,
               'supports_rpm': False, 'object_settings': True}

    class MappedWireAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            body = create_payload(**values, **options)
            sent.append(body)
            return '89'

        def detail(self, remote_id):
            return {'id': 89, **sent[0]}

        def validate_created_config(self, remote, config, proxy, channel_type):
            validate_readback(remote, config, proxy, channel_type, **options)

    monkeypatch.setattr(worker, 'get_adapter', MappedWireAdapter)
    worker.execute_item(db, item)
    assert json.loads(sent[0]['model_mapping']) == expected
    assert sent[0]['models'] == f'{haiku},{opus}'
    assert root.get('/api/upload-templates').json()['items'][0]['channel_config'] == {'status': 2}
    assert template.channel_config == raw_config


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode'])
@pytest.mark.parametrize('entrypoint', ['supplement', 'reprepare'])
def test_provider_mapping_recomputed_for_new_intersection_without_rewriting_old_snapshot(db, login, catalog, family, entrypoint):
    receiving = configure_claude_mapping_catalog(db, catalog, family)
    haiku, opus, _ = receiving
    expected_haiku = 'anthropic/claude-haiku-4.5' if family == 'OpenRouter' else 'claude-haiku-4-5'
    root = login('root')
    response = root.post('/api/upload-templates', json=template_body(catalog, models=receiving))
    assert response.status_code == 201, response.text
    client = login('user')
    original = submit(client, catalog, models=[haiku, opus, 'key-only-model'])
    old_item = db.get(TaskItem, original['items'][0]['id'])
    old_snapshot = json.loads(json.dumps(old_item.snapshot))
    task = db.get(Task, original['id'])
    if entrypoint == 'supplement':
        old_item.status, task.status = 'succeeded', 'succeeded'
        db.commit()
        response = root.post('/api/upload-templates', json=template_body(catalog, index=1, models=[haiku]))
        assert response.status_code == 201, response.text
        result = client.post('/api/channels/' + old_item.channel_id + '/actions', json={'action': 'redistribute'})
    else:
        old_item.status, task.status = 'failed', 'failed'
        db.commit()
        assert root.patch('/api/upload-templates/' + response.json()['id'], json={'models': [haiku]}).status_code == 200
        result = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert result.status_code == 200, result.text
    fresh = db.get(TaskItem, result.json()['items'][0]['id'])
    assert fresh.snapshot['models'] == [haiku]
    assert fresh.snapshot['channel_config']['model_mapping'] == {haiku: expected_haiku}
    assert fresh.snapshot['template_config']['channel_config'].get('model_mapping', {}) == {}
    assert old_item.snapshot == old_snapshot


def test_user_overrides_defaults_proxy_and_preserves_zero_false_and_null(db, login, catalog):
    enable_extended_contract(db, catalog)
    root = login('root')
    root.post('/api/upload-templates', json=template_body(catalog, proxy='http://template:default-secret@proxy.example:81',
        channel_config={'account_info': {'balance_usd': 12, 'rpm': 99, 'prepaid': True}}))
    client = login('user')
    proxy = 'http://user:override-secret@other.example:82'
    result = submit(client, catalog, proxies=proxy, account_info={'balance_usd': 0, 'rpm': None, 'prepaid': False})
    item = db.get(TaskItem, result['items'][0]['id'])
    channel = db.get(Channel, item.channel_id)
    assert decrypt(channel.proxy_encrypted) == proxy
    assert channel.upload_settings['account_info'] == {'balance_usd': 0.0, 'rpm': None, 'prepaid': False}
    assert item.snapshot['channel_config']['account_info']['balance_usd'] == 0
    assert item.snapshot['channel_config']['account_info']['rpm'] is None
    assert item.snapshot['channel_config']['account_info']['prepaid'] is False
    assert item.snapshot['proxy_source'] == 'channel' and item.snapshot['proxy_requested']
    ledger = json.dumps([item.snapshot, channel.upload_settings,
                        db.get(Distribution, item.distribution_id).template_snapshot, db.get(Task, item.task_id).snapshot])
    assert 'override-secret' not in ledger and 'default-secret' not in ledger and proxy not in ledger


def test_rpm_protection_blocks_real_distribution_when_only_local_account_info_is_supported(db, login, catalog):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    declared = client.post('/api/uploads/simple-preview', json=upload(catalog, account_info={'rpm': 40, 'tpm': 2000})).json()
    assert declared['can_submit']
    protected = client.post('/api/uploads/simple-preview', json=upload(catalog, rpm_enabled=True, rpm_limit=40)).json()
    assert not protected['can_submit'] and not protected['targets'][0]['compatible']
    assert client.post('/api/uploads/simple-submit', json=upload(catalog,
        idempotency_key='unsupported-protection', rpm_enabled=True, rpm_limit=40)).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.parametrize('change', ['template_proxy', 'channel_proxy', 'settings', 'channel_config'])
def test_new_configuration_changes_stop_queued_write(db, login, catalog, monkeypatch, change):
    enable_extended_contract(db, catalog)
    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog,
        proxy='http://template:before@proxy.example:81')).json()
    result = submit(login('user'), catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    row = db.get(SiteUploadTemplate, template['id'])
    channel = db.get(Channel, item.channel_id)
    if change == 'template_proxy':
        row.proxy_encrypted = encrypt('http://template:after@proxy.example:81')
    elif change == 'channel_proxy':
        channel.proxy_encrypted = encrypt('http://user:after@proxy.example:81')
    elif change == 'settings':
        channel.upload_settings = {'account_info': {'rpm': 44}}
    else:
        row.channel_config = {'status': 1}
    db.commit()
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(worker.WriteStopped):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted


def test_worker_uses_enabled_status_and_only_user_proxy_for_adapter(db, login, catalog, monkeypatch):
    enable_extended_contract(db, catalog)
    root = login('root')
    proxy = 'http://proxy-user:runtime-only@proxy.example:81'
    root.post('/api/upload-templates', json=template_body(catalog, proxy=proxy,
        channel_config={'status': 1, 'priority': 4, 'weight': 2}))
    result = submit(login('user'), catalog, proxies=proxy)
    item = db.get(TaskItem, result['items'][0]['id'])
    sent = {}

    class MockAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            sent.update(values)
            return '98'

        def detail(self, remote_id):
            return {'id': 98, 'name': sent['name'], 'type': sent['channel_type'],
                    'status': sent['config']['status'], 'models': ','.join(sent['models']), 'group': sent['group']}

    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    worker.execute_item(db, item)
    assert sent['proxy'] == proxy and sent['config']['priority'] == 0
    assert sent['config']['credential_format'] == catalog[1].schema_config
    assert db.get(Distribution, item.distribution_id).status == 'enabled'
    assert proxy not in json.dumps(item.snapshot)


def test_unknown_create_uses_frozen_proxy_for_readback_after_template_deletion(db, login, catalog, monkeypatch):
    enable_extended_contract(db, catalog)
    root = login('root')
    proxy = 'http://historical:only-in-ciphertext@proxy.example:81'
    template = root.post('/api/upload-templates', json=template_body(catalog, proxy=proxy)).json()
    result = submit(login('user'), catalog, proxies=proxy)
    item = db.get(TaskItem, result['items'][0]['id'])
    assert decrypt(item.proxy_encrypted) == proxy
    item.stage, item.remote_write_attempted = 'create_sent', True
    db.delete(db.get(SiteUploadTemplate, template['id']))
    db.commit()
    observed = []

    class ReadbackOnly:
        def __init__(self, *args, **kwargs):
            pass

        def find_unique_name(self, name):
            return {'id': 345, 'name': name, 'type': 1, 'status': 2,
                    'models': ','.join(item.snapshot['models']), 'group': item.snapshot['routing_group']}

        def validate_created_config(self, remote, *, config, proxy, channel_type):
            observed.append(proxy)

    monkeypatch.setattr(worker, 'get_adapter', ReadbackOnly)
    worker.execute_item(db, item)
    assert observed == [proxy]
    assert db.get(Distribution, item.distribution_id).remote_id == '345'


def test_reprepare_retains_user_overrides_with_new_template_defaults(db, login, catalog):
    enable_extended_contract(db, catalog)
    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog,
        channel_config={'account_info': {'prepaid': True, 'rpm': 15}})).json()
    client = login('user')
    result = submit(client, catalog, account_info={'prepaid': False})
    task = db.get(Task, result['id'])
    item = db.get(TaskItem, result['items'][0]['id'])
    task.status, item.status = 'cancelled', 'cancelled'
    db.commit()
    changed = root.patch('/api/upload-templates/' + template['id'], json={
        'channel_config': {'priority': 17, 'account_info': {'prepaid': True, 'rpm': 30}},
        'proxy': 'http://new:after-reprepare@proxy.example:81'})
    assert changed.status_code == 200, changed.text
    response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert response.status_code == 200, response.text
    fresh = db.get(TaskItem, response.json()['items'][0]['id'])
    assert fresh.snapshot['channel_config']['priority'] == 0
    assert fresh.snapshot['channel_config']['account_info']['prepaid'] is False
    assert fresh.snapshot['channel_config']['account_info']['rpm'] is None
    assert fresh.proxy_encrypted is None


def test_alternate_same_type_format_and_single_mode_are_honored(db, login, catalog):
    from app.newapi_formats import get_format_specs
    specs = [s for s in get_format_specs() if s['family'] == 'AWS']
    cat, fmt, sites = catalog
    cat.name = cat.family = 'AWS'
    fmt.code, fmt.schema_config = specs[0]['code'], specs[0]['schema_config']
    alternate = CredentialFormat(id=uid(), category_id=cat.id, code=specs[1]['code'], name='AWS API',
        version='1', enabled=True, schema_config=specs[1]['schema_config'])
    db.add(alternate)
    merged_spec = next(s for s in specs if s['code'] == 'newapi-33-aws-bedrock-v1')
    merged = CredentialFormat(id=uid(), category_id=cat.id, code=merged_spec['code'], name=merged_spec['name'],
        version='1', enabled=True, schema_config=merged_spec['schema_config'])
    db.add(merged)
    for site in sites:
        site.capabilities = {**site.capabilities, 'formats': [s['code'] for s in specs], 'channel_types': [33]}
    db.commit()
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    choices = client.get('/api/uploads/options').json()['items'][0]['formats']
    assert {value['id'] for value in choices} == {fmt.id, alternate.id, merged.id}
    result = submit(client, catalog, format_id=alternate.id, upload_mode='single', credentials='aws-api-token|us-east-1')
    item = db.get(TaskItem, result['items'][0]['id'])
    assert db.get(Channel, item.channel_id).format_id == alternate.id
    assert item.snapshot['channel_type'] == 33
    invalid = client.post('/api/uploads/simple-preview', json=upload(catalog,
        format_id=alternate.id, upload_mode='single', credentials='other-key|us-east-1\nthird-key|us-west-2')).json()
    assert not invalid['can_submit'] and any('单密钥' in error for error in invalid['errors'])


def test_missing_templates_blocks_simple_upload(db, login, catalog):
    client = login('user')
    result = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert not result['can_submit'] and result['targets'] == []
    assert client.post('/api/uploads/simple-submit', json=upload(catalog, idempotency_key='none-template')).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_simple_submit_freezes_each_site_configuration_and_masks_secrets(db, login, catalog):
    templates = add_templates(login('root'), catalog)
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert preview['can_submit'] and preview['target_count'] == 2
    assert 'test-credential-alpha' not in json.dumps(preview)
    result = submit(client, catalog, configuration_revision=preview['configuration_revision'])
    assert result['total'] == 2
    channels = list(db.scalars(select(Channel)))
    assert len(channels) == 1 and channels[0].upload_mode == 'template'
    assert channels[0].models == ['model-a', 'model-b']
    for template in templates:
        dist = db.scalar(select(Distribution).where(Distribution.site_id == template['site_id']))
        item = db.scalar(select(TaskItem).where(TaskItem.distribution_id == dist.id))
        assert dist.models == template['models'] and dist.routing_group == template['routing_group']
        assert dist.upload_template_id == template['id'] and dist.template_version == 1
        assert item.snapshot['models'] == template['models'] and item.snapshot['remark'] == template['remark']
        assert item.snapshot['enable_strategy'] == 'disabled'


def test_explicit_notes_override_defaults_and_duplicate_rows(login, catalog):
    add_templates(login('root'), catalog)
    result = login('user').post('/api/uploads/simple-preview', json=upload(catalog,
        credentials='test-alpha\ntest-alpha', remarks='same note')).json()
    assert result['can_submit'] and result['original_count'] == 2 and result['valid_count'] == 1
    assert result['duplicate_count'] == 1


def test_revision_rejects_stale_preview_and_same_nonce_is_idempotent(db, login, catalog):
    root = login('root')
    templates = add_templates(root, catalog)
    client = login('user')
    old = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()['configuration_revision']
    root.patch('/api/upload-templates/' + templates[0]['id'], json={'remark': 'new note'})
    response = client.post('/api/uploads/simple-submit', json=upload(catalog,
        idempotency_key='stale-revision-key', configuration_revision=old))
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    first = submit(client, catalog)
    root.patch('/api/upload-templates/' + templates[0]['id'], json={'enabled': False})
    second = submit(client, catalog, configuration_revision=old)
    assert first['id'] == second['id']
    assert client.post('/api/uploads/simple-submit', json=upload(catalog,
        credentials='different-key', idempotency_key='template-test-0001')).status_code == 409


def test_changed_template_repeated_credentials_conflicts(db, login, catalog):
    root = login('root')
    templates = add_templates(root, catalog)
    client = login('user')
    submit(client, catalog)
    root.patch('/api/upload-templates/' + templates[0]['id'], json={'models': ['model-b']})
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert not preview['can_submit'] and preview['conflict_count'] == 1
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


def test_new_template_can_redistribute_without_channel_union_equality(db, login, catalog):
    root = login('root')
    add_templates(root, catalog, count=1)
    client = login('user')
    first = submit(client, catalog)
    root.post('/api/upload-templates', json=template_body(catalog, index=1))
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert preview['can_submit'] and preview['rows'][0]['status'] == 'redistribute'
    second = submit(client, catalog, idempotency_key='template-test-0002')
    assert second['total'] == 1 and second['items'][0]['site_id'] == catalog[2][1].id
    assert second['items'][0]['channel_id'] == first['items'][0]['channel_id']
    assert db.scalar(select(func.count()).select_from(Channel)) == 1


def test_invalid_enabled_template_blocks_all_and_disabled_is_skipped(db, login, catalog):
    templates = add_templates(login('root'), catalog)
    row = db.get(SiteUploadTemplate, templates[1]['id'])
    row.models = ['no-longer-supported']
    db.commit()
    client = login('user')
    result = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert not result['can_submit'] and not result['can_submit_partial']
    assert not result['targets'][1]['compatible']
    row.enabled = False
    db.commit()
    result = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert result['can_submit'] and result['target_count'] == 1 and result['skipped_targets']


@pytest.mark.parametrize('change', ['disable', 'delete', 'models', 'version'])
def test_worker_stops_stale_template_before_any_write(db, login, catalog, monkeypatch, change):
    add_templates(login('root'), catalog)
    result = submit(login('user'), catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    template = db.get(SiteUploadTemplate, item.snapshot['upload_template_id'])
    if change == 'disable':
        template.enabled = False
    elif change == 'delete':
        db.delete(template)
    elif change == 'models':
        template.models = ['model-b']
    else:
        template.version += 1
    db.commit()
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: object())
    with pytest.raises(worker.WriteStopped, match='模板'):
        worker.execute_item(db, item)


def test_worker_uses_template_group_and_rechecks_at_http_callback(db, login, catalog, monkeypatch):
    add_templates(login('root'), catalog)
    result = submit(login('user'), catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    seen = []

    class MockAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            assert values['group'] == 'route-a' and values['models'] == ['model-a']
            assert values['remark'] == 'default note 0'
            template = db.get(SiteUploadTemplate, item.snapshot['upload_template_id'])
            template.enabled = False
            db.commit()
            self.before_write()
            seen.append(values)

    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    with pytest.raises(worker.WriteStopped, match='模板'):
        worker.execute_item(db, item)
    assert not seen


def test_worker_creates_disabled_using_each_target_snapshot(db, login, catalog, monkeypatch):
    add_templates(login('root'), catalog)
    result = submit(login('user'), catalog)
    sent = {}

    class MockAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            sent.update(values)
            return '77'

        def detail(self, remote_id):
            return {'id': 77, 'name': sent['name'], 'type': 1, 'status': 2,
                    'models': ','.join(sent['models']), 'group': sent['group']}

    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    for entry in result['items']:
        item = db.get(TaskItem, entry['id'])
        worker.execute_item(db, item)
        dist = db.get(Distribution, item.distribution_id)
        assert dist.status == 'disabled' and dist.models == item.snapshot['models']
        assert dist.routing_group == item.snapshot['routing_group']


def test_deleted_template_still_allows_unknown_write_readonly_reconciliation(db, login, catalog, monkeypatch):
    root = login('root')
    add_templates(root, catalog)
    result = submit(login('user'), catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    item.stage = 'create_sent'
    db.commit()
    response = root.delete('/api/upload-templates/' + item.snapshot['upload_template_id'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert not db.get(Site, item.site_id).enabled

    class MockAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def find_unique_name(self, name):
            return {'id': 77, 'name': name, 'type': 1, 'status': 2,
                    'models': ','.join(item.snapshot['models']), 'group': item.snapshot['routing_group']}

    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    worker.execute_item(db, item)
    assert db.get(Distribution, item.distribution_id).remote_id == '77'


def test_channel_action_redistribution_cannot_fallback_to_untemplated_site(db, login, catalog):
    root = login('root')
    add_templates(root, catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    for item in db.scalars(select(TaskItem)):
        item.status = 'succeeded'
    db.commit()
    channel_id = result['items'][0]['channel_id']
    assert client.post('/api/channels/' + channel_id + '/actions', json={'action': 'redistribute'}).status_code == 422
    root.post('/api/upload-templates', json=template_body(catalog, index=1))
    forbidden = client.post('/api/channels/' + channel_id + '/actions',
                            json={'action': 'redistribute', 'site_ids': [catalog[2][2].id]})
    assert forbidden.status_code == 422
    response = client.post('/api/channels/' + channel_id + '/actions', json={'action': 'redistribute'})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 1 and response.json()['items'][0]['site_id'] == catalog[2][1].id
    dist = db.scalar(select(Distribution).where(Distribution.site_id == catalog[2][1].id))
    assert dist.models == ['model-b'] and dist.routing_group == 'route-b'


def test_advanced_upload_cannot_bypass_category_templates(login, catalog):
    add_templates(login('root'), catalog, count=1)
    body = {**upload(catalog, credentials='another-new-key'), 'format_id': catalog[1].id, 'models': ['model-a']}
    client = login('user')
    preview = client.post('/api/uploads/preview', json=body).json()
    assert not preview['can_submit'] and any('简化上传' in error for error in preview['errors'])
    assert client.post('/api/uploads/submit', json={**body, 'idempotency_key': 'advanced-bypass-test'}).status_code == 422


def test_template_channel_rejects_global_model_override_and_rotation_preserves_site_values(db, login, catalog):
    add_templates(login('root'), catalog)
    client = login('user')
    result = submit(client, catalog)
    for i, item in enumerate(db.scalars(select(TaskItem))):
        item.status = 'succeeded'
        dist = db.get(Distribution, item.distribution_id)
        dist.remote_id = str(100 + i)
        dist.status = 'disabled'
    db.commit()
    channel_id = result['items'][0]['channel_id']
    assert client.patch('/api/channels/' + channel_id, json={'models': ['model-a']}).status_code == 422
    response = client.post('/api/channels/' + channel_id + '/rotate', json={'key': 'rotated-test-key'})
    assert response.status_code == 200, response.text
    task_id = response.json()['task_id']
    for item in db.scalars(select(TaskItem).where(TaskItem.task_id == task_id)):
        dist = db.get(Distribution, item.distribution_id)
        assert item.snapshot['models'] == dist.models
        assert item.snapshot['routing_group'] == dist.routing_group
        assert item.snapshot['remark'] == dist.template_snapshot['effective_remark']
        assert 'upload_template_id' not in item.snapshot


def test_reprepare_unsent_cancelled_items_uses_current_template_and_keeps_history(db, login, catalog):
    root = login('root')
    templates = add_templates(root, catalog)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'cancelled'
    for item in db.scalars(select(TaskItem).where(TaskItem.task_id == task.id)):
        item.status = 'cancelled'
    db.commit()
    root.patch('/api/upload-templates/' + templates[0]['id'], json={'models': ['model-b'], 'remark': 'new default'})
    assert client.get('/api/tasks/' + task.id).json()['can_reprepare_templates']
    response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert response.status_code == 200, response.text
    new_task = response.json()
    assert new_task['id'] != task.id and new_task['total'] == 2
    assert not client.get('/api/tasks/' + task.id).json()['can_reprepare_templates']
    old_items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id).execution_options(populate_existing=True)))
    assert all(item.stage == 'superseded' for item in old_items)
    new_item = db.scalar(select(TaskItem).where(TaskItem.task_id == new_task['id'], TaskItem.site_id == templates[0]['site_id']))
    assert new_item.snapshot['models'] == ['model-b'] and new_item.snapshot['remark'] == 'new default'
    assert new_item.snapshot['template_version'] == 2
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


@pytest.mark.parametrize('stage', ['create_sent', 'created_pending_verification', 'reconcile'])
def test_reprepare_never_reposts_potential_remote_creates(db, login, catalog, stage):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'failed'
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status, item.stage = 'failed', stage
    db.commit()
    assert client.post('/api/tasks/' + task.id + '/reprepare-templates').status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_reprepare_preserves_ownership_and_blocks_missing_template(db, login, catalog):
    templates = add_templates(login('root'), catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'cancelled'
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status = 'cancelled'
    db.delete(db.get(SiteUploadTemplate, templates[0]['id']))
    db.commit()
    assert login('other_user').post('/api/tasks/' + task.id + '/reprepare-templates').status_code == 404
    assert client.post('/api/tasks/' + task.id + '/reprepare-templates').status_code == 422


def test_options_and_preview_reject_mixed_formats_in_category(db, login, catalog):
    templates = add_templates(login('root'), catalog)
    fmt = CredentialFormat(id=uid(), category_id=catalog[0].id, code='api_key-v1', name='Future format', version='2', enabled=True)
    db.add(fmt)
    db.flush()
    db.get(SiteUploadTemplate, templates[1]['id']).format_id = fmt.id
    db.commit()
    response = login('user').post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert not response['can_submit'] and any('不同凭据格式' in issue for issue in response['errors'])


@pytest.mark.parametrize('write_marker', [True, False])
def test_unknown_create_reconcile_cancel_cannot_erase_write_history(db, login, catalog, write_marker):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'failed'
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status, item.stage = 'needs_review', 'create_sent'
    item.remote_write_attempted = write_marker
    db.commit()
    assert client.post('/api/tasks/' + task.id + '/reconcile').status_code == 200
    cancelled = client.post('/api/tasks/' + task.id + '/cancel')
    assert cancelled.status_code == 200
    assert cancelled.json()['counts']['needs_review'] == 1
    assert not cancelled.json()['can_reprepare_templates']
    assert client.post('/api/tasks/' + task.id + '/reprepare-templates').status_code == 409
    db.refresh(item)
    assert item.remote_write_attempted == write_marker and item.stage == 'reconcile'
    assert db.scalar(select(func.count()).select_from(Task)) == 1


def test_reprepare_checks_other_item_history_even_if_current_item_unsent(db, login, catalog):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'cancelled'
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status = 'cancelled'
    db.add(TaskItem(task_id=task.id, channel_id=item.channel_id, distribution_id=item.distribution_id, site_id=item.site_id,
                   operation='create', status='failed', stage='queued', remote_write_attempted=True))
    db.commit()
    assert client.post('/api/tasks/' + task.id + '/reprepare-templates').status_code == 409


@pytest.mark.parametrize(('operation', 'snapshot', 'allowed'), [
    ('test', {'test_source': 'local'}, True), ('test', {}, False),
    ('test', {'test_source': 'remote'}, False), ('create', {'test_source': 'local'}, False),
])
def test_reprepare_ignores_only_local_model_probe_history(db, login, catalog, operation, snapshot, allowed):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    result = submit(client, catalog)
    task = db.get(Task, result['id'])
    task.status = 'cancelled'
    item = db.get(TaskItem, result['items'][0]['id'])
    item.status, item.stage = 'cancelled', 'queued'
    db.add(TaskItem(task_id=task.id, channel_id=item.channel_id, distribution_id=item.distribution_id,
        site_id=item.site_id, operation=operation, snapshot=snapshot,
        status='succeeded', stage='complete', remote_write_attempted=True))
    db.commit()
    response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert response.status_code == (200 if allowed else 409), response.text


@pytest.mark.parametrize('recovery', ['retry', 'reprepare'])
def test_inventory_remains_disabled_with_enabled_template_after_recovery(db, login, catalog, monkeypatch, recovery):
    enable_extended_contract(db, catalog)
    root = login('root')
    template = root.post('/api/upload-templates', json=template_body(catalog,
        channel_config={'status': 1, 'priority': 4})).json()
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, inventory=True)).json()
    assert preview['can_submit'] and preview['inventory'] and preview['enable_strategy'] == 'disabled'
    result = submit(client, catalog, inventory=True)
    task = db.get(Task, result['id'])
    item = db.get(TaskItem, result['items'][0]['id'])
    channel = db.get(Channel, item.channel_id)
    assert channel.upload_settings == {'inventory': True}
    assert item.snapshot['upload_settings_snapshot'] == {'inventory': True}
    assert item.snapshot['channel_config']['status'] == 2
    assert item.snapshot['enable_strategy'] == 'disabled'
    assert db.get(Distribution, item.distribution_id).template_snapshot['effective_channel_config']['status'] == 2
    task.status = item.status = 'failed'
    db.commit()
    if recovery == 'retry':
        response = client.post('/api/tasks/' + task.id + '/retry')
    else:
        changed = root.patch('/api/upload-templates/' + template['id'],
                             json={'channel_config': {'status': 1, 'priority': 9}})
        assert changed.status_code == 200, changed.text
        response = client.post('/api/tasks/' + task.id + '/reprepare-templates')
    assert response.status_code == 200, response.text
    fresh = db.get(TaskItem, response.json()['items'][0]['id'], populate_existing=True)
    assert fresh.snapshot['channel_config']['status'] == 2 and fresh.snapshot['enable_strategy'] == 'disabled'
    sent = []

    class DisabledOnlyAdapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            assert values['config']['status'] == 2
            sent.append(values)
            return '918'

        def detail(self, remote_id):
            return {'id': 918, 'name': sent[-1]['name'], 'type': 1, 'status': 2,
                    'models': ','.join(sent[-1]['models']), 'group': sent[-1]['group']}

    monkeypatch.setattr(worker, 'get_adapter', DisabledOnlyAdapter)
    worker.execute_item(db, fresh)
    assert len(sent) == 1 and db.get(Distribution, fresh.distribution_id).status == 'disabled'


def test_inventory_unknown_readback_rejects_enabled_remote_without_another_create(db, login, catalog, monkeypatch):
    enable_extended_contract(db, catalog)
    login('root').post('/api/upload-templates', json=template_body(catalog, channel_config={'status': 1}))
    result = submit(login('user'), catalog, inventory=True)
    item = db.get(TaskItem, result['items'][0]['id'])
    item.stage, item.remote_write_attempted = 'create_sent', True
    db.commit()

    class ReadbackOnly:
        def __init__(self, *args, **kwargs):
            pass

        def find_unique_name(self, name):
            return {'id': 919, 'name': name, 'type': 1, 'status': 1,
                    'models': ','.join(item.snapshot['models']), 'group': item.snapshot['routing_group']}

        def create(self, **values):
            pytest.fail('Uncertain inventory create must never be resent')

    monkeypatch.setattr(worker, 'get_adapter', ReadbackOnly)
    with pytest.raises(worker.RemoteError, match='配置与提交快照不同') as failure:
        worker.execute_item(db, item)
    assert failure.value.unknown
    assert db.get(Distribution, item.distribution_id).remote_id is None


def test_default_inventory_retains_legacy_hash_and_dedup_settings(db, login, catalog):
    add_templates(login('root'), catalog, count=1)
    client = login('user')
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog)).json()
    assert preview['group_tag'] is None and preview['inventory'] is False
    first = submit(client, catalog)
    task = db.get(Task, first['id'])
    old_values = SimpleSubmitInput(**upload(catalog, idempotency_key='template-test-0001')).model_dump(
        exclude={'idempotency_key', 'configuration_revision', 'inventory', 'batch_token', 'api_base_url'})
    assert task.request_hash == fingerprint(json.dumps({'mode': 'template', **old_values}, sort_keys=True, ensure_ascii=False))
    repeated = submit(client, catalog, inventory=False)
    assert repeated['id'] == first['id']
    assert db.get(Channel, first['items'][0]['channel_id']).upload_settings == {}
    check = client.post('/api/uploads/simple-preview', json=upload(catalog,
        inventory=False, batch_token='new-batch-token-unchanged-settings')).json()
    assert not check['can_submit'] and check['conflict_count'] == 0
    assert check['rows'][0]['status'] == 'already_distributed'


@pytest.mark.parametrize('invalid', [{'inventory': 1}, {'inventory': 'true'}, {'inventory': None},
                                    {'batch_token': 'short'}, {'batch_token': 'x' * 121}, {'group_tag': 'user-chosen'}])
def test_simple_upload_rejects_non_boolean_inventory_or_untrusted_batch_inputs(login, catalog, invalid):
    assert login('user').post('/api/uploads/simple-preview', json=upload(catalog, **invalid)).status_code == 422


@pytest.mark.parametrize('endpoint', ['simple-preview', 'simple-submit'])
def test_upload_label_cannot_be_supplied_by_client(db, login, catalog, endpoint):
    client = login('user')
    body = upload(catalog, batch_token='server-generated-label-check')
    if endpoint == 'simple-submit':
        body['idempotency_key'] = 'server-generated-label-submit'
    for field in ('tag', 'group_tag', 'label', 'user_id'):
        response = client.post(f'/api/uploads/{endpoint}', json={**body, field: 'client-chosen'})
        assert response.status_code == 422, response.text
        assert any(error['loc'] == ['body', field] and error['type'] == 'extra_forbidden'
                   for error in response.json()['detail'])
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 0


def test_batch_label_is_authorized_scoped_stable_and_bounded(db, login, client, catalog, users):
    add_templates(login('root'), catalog, count=1)
    params = {'category_id': catalog[0].id, 'batch_token': 'label-only-batch-token'}
    assert client.get('/api/uploads/label', params=params).status_code == 401
    assert login('root').get('/api/uploads/label', params=params).status_code == 403
    user_client = login('user')
    first = user_client.get('/api/uploads/label', params=params)
    assert first.status_code == 200, first.text
    assert first.json() == user_client.get('/api/uploads/label', params=params).json()
    assert first.json()['group_tag'] != login('admin').get('/api/uploads/label', params=params).json()['group_tag']
    category = Category(id=uid(), name='Anthropic', family='Anthropic', active=True)
    assert first.json()['group_tag'] != batch_group_tag(db, users['user'], category, params['batch_token'])
    users['user'].username = 'u' * 64
    db.commit()
    bounded = user_client.get('/api/uploads/label', params=params).json()['group_tag']
    assert len(bounded) <= 80 and bounded.endswith(first.json()['group_tag'].split('-')[-1])
    assert user_client.get('/api/uploads/label', params={**params, 'batch_token': 'short'}).status_code == 422
    assert user_client.get('/api/uploads/label', params={**params, 'category_id': uid()}).status_code == 422
    catalog[0].active = False
    db.commit()
    assert user_client.get('/api/uploads/label', params=params).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 0


def test_batch_label_matches_preview_group_and_retries_without_token_storage(db, login, catalog, users):
    templates = add_templates(login('root'), catalog, count=1)
    client = login('user')
    token = 'private-client-batch-identity'
    params = {'category_id': catalog[0].id, 'batch_token': token}
    label = client.get('/api/uploads/label', params=params).json()['group_tag']
    preview = client.post('/api/uploads/simple-preview', json=upload(catalog, batch_token=token)).json()
    assert preview['group_tag'] == label
    first = submit(client, catalog, batch_token=token)
    assert first['group_tag'] == label
    task = db.get(Task, first['id'])
    assert db.get(UploadGroup, task.group_id).tag == label
    channel = db.get(Channel, first['items'][0]['channel_id'])
    assert channel.upload_settings == {}
    assert token not in json.dumps([task.snapshot, channel.upload_settings,
                                   db.get(TaskItem, first['items'][0]['id']).snapshot])
    users['user'].username = 'renamed-user'
    db.commit()
    assert client.get('/api/uploads/label', params=params).json()['group_tag'] == label
    second = submit(client, catalog, batch_token=token, idempotency_key='different-network-retry-nonce')
    assert second['id'] == first['id'] and second['group_tag'] == label
    conflict = client.post('/api/uploads/simple-submit', json=upload(catalog, batch_token=token,
        credentials='different-batch-content', idempotency_key='different-content-nonce'))
    assert conflict.status_code == 409
    assert db.scalar(select(func.count()).select_from(Task)) == 1
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1
    db.get(SiteUploadTemplate, templates[0]['id']).enabled = False
    db.commit()
    assert submit(client, catalog, batch_token=token, idempotency_key='retry-after-template-change')['id'] == first['id']


def test_batch_label_does_not_require_default_template_readiness(db, login, catalog):
    client = login('user')
    params = {'category_id': catalog[0].id, 'batch_token': 'label-before-template-ready'}
    label = client.get('/api/uploads/label', params=params)
    assert label.status_code == 200, label.text
    assert not client.post('/api/uploads/simple-preview', json=upload(catalog)).json()['can_submit']
    enable_extended_contract(db, catalog)
    root = login('root')
    created = root.post('/api/upload-templates', json=template_body(catalog, channel_config={'status': 1}))
    assert created.status_code == 201, created.text
    site = catalog[2][0]
    site.capabilities = {**site.capabilities, 'can_toggle': False}
    db.commit()
    assert client.get('/api/uploads/label', params=params).json() == label.json()
    assert not client.post('/api/uploads/simple-preview', json=upload(catalog)).json()['can_submit']
    inventory = client.post('/api/uploads/simple-preview', json=upload(catalog, batch_token=params['batch_token'],
                                                                      inventory=True)).json()
    assert inventory['can_submit'] and inventory['group_tag'] == label.json()['group_tag']


def test_batch_identity_deduplicates_supplemental_distribution_without_new_group(db, login, catalog):
    root = login('root')
    add_templates(root, catalog, count=1)
    client = login('user')
    first = submit(client, catalog)
    assert root.post('/api/upload-templates', json=template_body(catalog, index=1)).status_code == 201
    supplemental = submit(client, catalog, batch_token='supplemental-batch-token', idempotency_key='supplemental-nonce-one')
    assert db.get(Task, supplemental['id']).group_id is None
    repeated = submit(client, catalog, batch_token='supplemental-batch-token', idempotency_key='supplemental-nonce-two')
    assert repeated['id'] == supplemental['id'] and repeated['group_tag'] == supplemental['group_tag']
    assert repeated['items'][0]['channel_id'] == first['items'][0]['channel_id']
    assert db.scalar(select(func.count()).select_from(Task)) == 2
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1
    assert db.scalar(select(func.count()).select_from(Distribution)) == 2


def test_label_format_scope_and_numeric_beijing_timestamp(db, login, catalog, users, monkeypatch):
    monkeypatch.setattr('app.upload_templates.utcnow', lambda: datetime(2026, 9, 9, 16, 0, 1, tzinfo=UTC).replace(tzinfo=None))
    category = Category(id=uid(), name='AWS', family='AWS', active=True)
    db.add(category)
    db.flush()
    formats = {}
    for spec in get_format_specs():
        kind = spec['schema_config']['type']
        if kind in ('aws_bedrock', 'aws_claude'):
            fmt = CredentialFormat(id=uid(), category_id=category.id, code=spec['code'], name=spec['name'],
                                   version=spec['version'], schema_config=spec['schema_config'], enabled=True)
            db.add(fmt)
            formats[kind] = fmt
    db.commit()
    client = login('user')
    params = {'category_id': category.id, 'batch_token': 'same-label-batch-token'}
    for kind, slug in [('aws_bedrock', 'AWS-bedrock'), ('aws_claude', 'AWS-claude')]:
        response = client.get('/api/uploads/label', params={**params, 'format_id': formats[kind].id})
        assert response.status_code == 200, response.text
        assert re.fullmatch(f"{users['user'].display_id}-{slug}-20260910-00:00:01-[a-z0-9]{{4}}", response.json()['group_tag'])
        assert response.json() == client.get('/api/uploads/label', params={**params, 'format_id': formats[kind].id}).json()
    assert client.get('/api/uploads/label', params={**params, 'format_id': catalog[1].id}).status_code == 422
    assert client.get('/api/uploads/label', params={**params, 'format_id': uid()}).status_code == 422
    formats['aws_claude'].enabled = False
    db.commit()
    assert client.get('/api/uploads/label', params={**params, 'format_id': formats['aws_claude'].id}).status_code == 422


def test_simple_and_advanced_fallback_new_labels_preserve_idempotent_groups(db, login, catalog, users):
    client = login('user')
    advanced_body = {'category_id': catalog[0].id, 'format_id': catalog[1].id,
                     'credentials': 'another-upload-key', 'models': ['model-a'], 'idempotency_key': 'advanced-label-nonce'}
    advanced = client.post('/api/uploads/submit', json=advanced_body)
    assert advanced.status_code == 200, advanced.text
    add_templates(login('root'), catalog, count=1)
    simple = submit(client, catalog)
    for result in (simple, advanced.json()):
        group = db.get(UploadGroup, result['group_id'])
        assert re.fullmatch(f"{users['user'].display_id}-OpenAI-api-key-[0-9]{{8}}-[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}-[a-z0-9]{{4}}", group.tag)
        assert group.name == group.tag
    assert submit(client, catalog)['id'] == simple['id']
    assert client.post('/api/uploads/submit', json=advanced_body).json()['id'] == advanced.json()['id']
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 2
