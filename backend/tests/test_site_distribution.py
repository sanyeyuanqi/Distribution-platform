"""Distribution gates use every enabled receiving template, not the site fallback group."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest
from app.db import SessionLocal, utcnow
from app.models import (
    AuditEvent,
    Category,
    CredentialFormat,
    Site,
    SiteUploadTemplate,
    User,
)
from app.routers import sites, upload_templates
from app.security import encrypt
from app.site_distribution import distribution_readiness
from sqlalchemy import select


def add_enabled_template(db, site, *, models=('model-a',), group='default'):
    """Give unrelated site API tests an actually usable receiving template."""
    category = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    if category is None:
        category = Category(name='OpenAI', family='OpenAI', active=True)
        db.add(category)
        db.flush()
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id,
                                                   CredentialFormat.code == 'api_key-v1'))
    if fmt is None:
        fmt = CredentialFormat(category_id=category.id, code='api_key-v1', name='API Key', version='1',
            schema_config={'type': 'api_key', 'remote_type': 1}, enabled=True)
        db.add(fmt)
        db.flush()
    template = SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id,
        name='Receiving template', enabled=True, models=list(models), routing_group=group, channel_config={'status': 2})
    db.add(template)
    db.flush()
    return template


@pytest.fixture
def ready_site(db, users, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Readiness tests never contact real sites or invoke inference')
    monkeypatch.setattr('app.adapters.silicon.safe_request', forbidden)
    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    cap = {'read': 'supported', 'create': 'supported', 'can_write': True, 'can_toggle': True,
           'formats': ['api_key-v1'], 'channel_types': [1], 'channel_config': 'supported',
           'models': ['model-a', 'model-b'], 'groups': ['普通', '高倍率'], 'custom_models': False}
    category = Category(name='OpenAI', family='OpenAI', active=True)
    db.add(category)
    db.flush()
    fmt = CredentialFormat(category_id=category.id, code='api_key-v1', name='API Key', version='1',
        schema_config={'type': 'api_key', 'remote_type': 1}, enabled=True)
    site = Site(name='Template routing', prefix='GATED', base_url='https://fixture.invalid', adapter='new-api-v1',
        seller_user_id='71', token_encrypted=encrypt('fixture-management-token'), routing_group='default',
        enabled=False, verified_at=utcnow(), health='healthy', capabilities=deepcopy(cap))
    db.add_all([fmt, site])
    db.flush()
    template = SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id,
        name='Primary template', enabled=True, models=['model-a'], routing_group='普通,高倍率', channel_config={'status': 2})
    db.add(template)
    db.commit()
    state = SimpleNamespace(site=site, category=category, fmt=fmt, template=template, cap=cap, calls=[], error=None)
    class Adapter:
        def verify(self):
            state.calls.append('read-only verify')
            if state.error:
                raise state.error
            return {'identity': {'id': '71'}, 'capabilities': deepcopy(state.cap), 'verified_version': 'v0.13.2'}
    monkeypatch.setattr(sites, 'get_adapter', lambda row: Adapter())
    return state


def site_response(client, site_id):
    return next(item for item in client.get('/api/sites').json()['items'] if item['id'] == site_id)


def test_valid_template_groups_allow_enable_without_default_group(login, db, ready_site):
    root = login('root')
    before = site_response(root, ready_site.site.id)
    assert before['enabled'] is False and before['distribution_ready'] is True
    assert before['distribution_issues'] == [] and before['enabled_template_count'] == 1
    for _ in range(2):
        result = root.patch('/api/sites/' + ready_site.site.id, json={'enabled': True}).json()
        assert result['enabled'] is True and result['distribution_ready'] is True
        assert result['routing_group'] == 'default'  # It remains the legacy fallback; it was not selected by the template.
    assert len(ready_site.calls) == 2
    ordinary = login('user').get('/api/sites').json()['items'][0]
    assert set(ordinary) == {'id', 'display_id', 'name', 'health', 'enabled'}
    db.refresh(ready_site.template)
    assert ready_site.template.routing_group == '普通,高倍率'


@pytest.mark.parametrize(('problem', 'message'), [
    ('group', '站点不允许路由组'), ('models', '不支持的模型'), ('format', '站点不支持所选凭据格式'),
    ('type', '不支持所选渠道类型'), ('permission', '没有创建渠道权限'), ('empty_models', '模板尚未配置模型'),
    ('toggle', '启用权限'), ('disabled_format', '分类或凭据格式已停用'), ('category', '分类或凭据格式已停用'),
])
def test_each_enabled_template_must_pass_the_same_actual_upload_rules(login, db, ready_site, problem, message):
    case = ready_site
    if problem == 'group': case.template.routing_group = '普通,不可用组'
    elif problem == 'models': case.template.models = ['unknown-model']
    elif problem == 'format': case.cap['formats'] = []
    elif problem == 'type': case.cap['channel_types'] = [14]
    elif problem == 'permission': case.cap.update(can_write=False, create='permission_denied')
    elif problem == 'empty_models': case.template.models = []
    elif problem == 'toggle':
        case.cap['can_toggle'] = False
        case.template.channel_config = {'status': 1}
    elif problem == 'disabled_format': case.fmt.enabled = False
    else: case.category.active = False
    db.commit()
    response = login('root').patch('/api/sites/' + case.site.id, json={'enabled': True})
    assert response.status_code == 200
    data = response.json()
    assert data['enabled'] is False and data['distribution_ready'] is False
    assert any(message in issue for issue in data['distribution_issues'])
    assert case.calls == ['read-only verify']


def test_custom_models_and_created_disabled_status_respect_verified_capabilities(login, db, ready_site):
    ready_site.cap.update(custom_models=True, can_toggle=False)
    ready_site.template.models = ['new-model-not-in-catalog']
    db.commit()
    result = login('root').patch('/api/sites/' + ready_site.site.id, json={'enabled': True}).json()
    assert result['enabled'] and result['distribution_ready']


def test_one_bad_enabled_template_blocks_even_when_another_is_valid_but_disabled_draft_does_not(login, db, ready_site):
    case = ready_site
    # Different legacy variants are enough to represent multiple receiving templates on the same site.
    second = SiteUploadTemplate(site_id=case.site.id, category_id=case.category.id, format_id=case.fmt.id,
        variant='historical', name='Second template', enabled=True, models=['model-b'], routing_group='invalid')
    db.add(second)
    db.commit()
    root = login('root')
    result = root.patch('/api/sites/' + case.site.id, json={'enabled': True}).json()
    assert not result['enabled'] and result['enabled_template_count'] == 2
    assert any('Second template' in issue for issue in result['distribution_issues'])
    second.enabled = False
    db.commit()
    result = root.patch('/api/sites/' + case.site.id, json={'enabled': True}).json()
    assert result['enabled'] and result['enabled_template_count'] == 1


@pytest.mark.parametrize('template_state', ['absent', 'disabled'])
def test_site_without_enabled_templates_can_be_enabled_after_successful_verify(login, db, ready_site, template_state):
    root = login('root')
    if template_state == 'absent':
        db.delete(ready_site.template)
    else:
        ready_site.template.enabled = False
    db.commit()
    before = site_response(root, ready_site.site.id)
    assert before['enabled'] is False and before['distribution_ready'] is True
    assert before['distribution_issues'] == [] and before['enabled_template_count'] == 0
    result = root.patch('/api/sites/' + ready_site.site.id, json={'enabled': True}).json()
    assert result['enabled'] is True and result['distribution_ready'] is True
    assert result['distribution_issues'] == [] and result['enabled_template_count'] == 0
    assert ready_site.calls == ['read-only verify']
    if template_state == 'disabled':
        db.refresh(ready_site.template)
        assert ready_site.template.enabled is False


def test_disabled_site_can_configure_enabled_template_before_explicit_site_enable(login, db, ready_site):
    case = ready_site
    db.delete(case.template)
    db.commit()
    root = login('root')
    result = root.post('/api/upload-templates', json={'site_id': case.site.id, 'category_id': case.category.id,
        'format_id': case.fmt.id, 'name': 'Prepared while stopped', 'models': ['model-a'], 'routing_group': '普通', 'enabled': True})
    assert result.status_code == 201, result.text
    assert result.json()['enabled'] is True and result.json()['ready'] is False
    assert result.json()['issues'] == ['站点已停用或归档']
    prepared = site_response(root, case.site.id)
    assert prepared['distribution_ready'] and not prepared['enabled']
    assert root.patch('/api/sites/' + case.site.id, json={'enabled': True}).json()['enabled']


@pytest.mark.parametrize('change', ['disable', 'delete'])
def test_removing_last_enabled_template_keeps_site_enabled(login, db, ready_site, change):
    case = ready_site
    case.site.enabled = True
    db.commit()
    root = login('root')
    path = '/api/upload-templates/' + case.template.id
    response = root.patch(path, json={'enabled': False}) if change == 'disable' else root.delete(path)
    assert response.status_code == 200
    result = site_response(root, case.site.id)
    assert result['enabled'] is True and result['distribution_ready'] is True
    assert result['distribution_issues'] == [] and result['enabled_template_count'] == 0
    if change == 'disable':
        assert root.patch(path, json={'enabled': True}).status_code == 200
        result = site_response(root, case.site.id)
        assert result['distribution_ready'] is True and result['enabled'] is True
    assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'site.distribution.stop')) is None


@pytest.mark.parametrize('change', ['permission', 'group', 'connection'])
def test_reverify_losing_readiness_stops_site_and_success_does_not_restart_it(login, db, ready_site, change):
    from app.adapters.silicon import RemoteError
    case = ready_site
    original = deepcopy(case.cap)
    case.site.enabled = True
    db.commit()
    if change == 'permission': case.cap.update(can_write=False, create='permission_denied')
    elif change == 'group': case.cap['groups'] = ['unrelated']
    else: case.error = RemoteError('sensitive upstream message', category='connection_error')
    root = login('root')
    result = root.post('/api/sites/' + case.site.id + '/verify').json()
    assert not result['enabled'] and not result['distribution_ready']
    assert 'sensitive upstream message' not in str(result)
    case.cap, case.error = original, None
    repaired = root.post('/api/sites/' + case.site.id + '/verify').json()
    assert repaired['distribution_ready'] and not repaired['enabled']


def test_get_readiness_is_read_only_and_unknown_does_not_claim_every_capability_unsupported(login, db, ready_site):
    case = ready_site
    case.site.capabilities = {'verification_error': {'category': 'connection_error', 'message': 'private string'}}
    case.site.verified_at = None
    case.site.health = 'healthy'  # Stale UI health never means verified.
    db.commit()
    before = deepcopy(case.site.capabilities)
    result = site_response(login('root'), case.site.id)
    assert result['distribution_issues'] == ['站点验证失败：无法连接目标站点，请检查网络及站点状态']
    assert result['enabled_template_count'] == 1 and not result['distribution_ready']
    assert case.calls == [] and case.site.capabilities == before


def test_unhealthy_or_archived_site_is_not_ready(db, ready_site):
    ready_site.site.health = 'connection_error'
    assert not distribution_readiness(db, ready_site.site)['distribution_ready']
    ready_site.site.health, ready_site.site.archived = 'healthy', True
    assert not distribution_readiness(db, ready_site.site)['distribution_ready']


def test_template_disable_and_site_enable_serialize_without_reverse_lock_order(db, users, ready_site, monkeypatch):
    case = ready_site
    root_id, site_id, template_id = users['root'].id, case.site.id, case.template.id
    verify_entered, release_verify, template_entered = Event(), Event(), Event()
    original_verify = sites.verify_site
    def paused_verify(row):
        verify_entered.set()
        assert release_verify.wait(10)
        return original_verify(row)
    monkeypatch.setattr(sites, 'verify_site', paused_verify)
    def enable():
        with SessionLocal() as session:
            return sites.update_site(site_id, sites.SitePatch(enabled=True), session.get(User, root_id), session)
    def disable_template():
        with SessionLocal() as session:
            template_entered.set()
            return upload_templates.patch(template_id, upload_templates.TemplatePatch(enabled=False),
                                          session.get(User, root_id), session)
    with ThreadPoolExecutor(max_workers=2) as executor:
        enabling = executor.submit(enable)
        assert verify_entered.wait(10)
        disabling = executor.submit(disable_template)
        assert template_entered.wait(10)
        release_verify.set()
        enabling.result(timeout=15)
        disabling.result(timeout=15)
    db.expire_all()
    assert not db.get(SiteUploadTemplate, template_id).enabled
    assert db.get(Site, site_id).enabled
