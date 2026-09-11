"""Site APIs expose template choices, never their internal capability contract."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.db import utcnow
from app.models import Site
from app.routers import sites
from app.security import encrypt
from sqlalchemy import select

VERSION = 'v1.0.0-rc.32-colin'
INTERNAL = {
    'read': 'supported', 'create': 'supported', 'can_write': True,
    'remote_permissions': {'secret_view': True, 'internal_marker': 'internal-only-data'},
    'channel_types': [1, 33, 41], 'formats': ['internal-format'],
    'usage_conversion': {'quota_per_unit': '500000', 'base_unit': 'USD'},
    'models': ['model-a', 'model-b', 'model-a'], 'groups': ['default', 'shared'],
    'custom_models': True,
}
OPTIONS = {'models': ['model-a', 'model-b'], 'groups': ['default', 'shared'], 'allow_custom_models': True,
           'routing_group_max_length': 160}


@pytest.fixture
def public_site(db, monkeypatch):
    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    monkeypatch.setattr(sites, 'get_adapter', lambda row: SimpleNamespace(verify=lambda: {
        'capabilities': deepcopy(INTERNAL), 'verified_version': VERSION}))
    site = Site(name='Visible site', prefix='VISIBLE', base_url='https://fixture.invalid',
        seller_user_id='71', token_encrypted=encrypt('fixture-token'), token_hint='masked',
        adapter='tcp-red-v1', enabled=True, health='healthy', verified_at=utcnow(),
        capabilities={**deepcopy(INTERNAL), 'verified_version': VERSION})
    db.add(site)
    db.commit()
    return site


def assert_admin_projection(value):
    assert 'capabilities' not in value
    assert value['verified_version'] == VERSION and value['verification_error'] is None
    assert value['template_options'] == OPTIONS
    assert not {'remote_permissions', 'channel_types', 'formats', 'can_write', 'usage_conversion'} & value.keys()
    assert 'internal-only-data' not in str(value) and 'internal-format' not in str(value)


@pytest.mark.parametrize('operation', ['list', 'create', 'patch', 'verify', 'delete'])
def test_all_site_responses_publish_only_narrow_template_projection(login, db, public_site, operation):
    root = login('root')
    path = '/api/sites/' + public_site.id
    if operation == 'list':
        response = root.get('/api/sites')
        value = response.json()['items'][0]
    elif operation == 'create':
        response = root.post('/api/sites', json={'name': 'Created site', 'prefix': 'CREATED',
            'base_url': 'https://other-fixture.invalid', 'seller_user_id': '71',
            'adapter': 'tcp-red-v1', 'token': 'mock-create-token', 'enabled': True})
        value = response.json()
    elif operation == 'patch':
        response = root.patch(path, json={'name': 'Updated site'})
        value = response.json()
    elif operation == 'verify':
        response = root.post(path + '/verify')
        value = response.json()
    else:
        response = root.request('DELETE', path, json={'confirmation': public_site.name})
        value = response.json()
    assert response.status_code == (201 if operation == 'create' else 200), response.text
    assert_admin_projection(value)
    db.expire_all()
    stored = db.get(Site, value['id'])
    assert stored.capabilities == {**INTERNAL, 'verified_version': VERSION}
    assert stored.token_encrypted is not None


@pytest.mark.parametrize('role', ['user', 'admin', 'other_user'])
def test_ordinary_roles_receive_only_basic_enabled_site_fields(login, public_site, role):
    response = login(role).get('/api/sites')
    assert response.status_code == 200
    assert response.json()['items'] == [{'id': public_site.id, 'display_id': public_site.display_id, 'name': public_site.name,
        'health': 'healthy', 'enabled': True}]


@pytest.mark.parametrize('failed', [False, True])
def test_unverified_or_failed_site_has_no_template_choices_even_with_stale_capabilities(login, db, public_site, failed):
    if failed:
        public_site.capabilities = {**public_site.capabilities, 'verification_error': {
            'category': 'protocol_error', 'reason': 'unsupported_version', 'observed_version': 'v1.0.0-rc.99',
            'endpoint': '/api/status', 'method': 'GET', 'message': 'secret-from-remote', 'body': 'private-body'}}
    else:
        public_site.verified_at = None
    db.commit()
    response = login('root').get('/api/sites')
    assert response.status_code == 200
    value = response.json()['items'][0]
    assert 'capabilities' not in value
    assert value['template_options'] == {'models': [], 'groups': [], 'allow_custom_models': False,
                                         'routing_group_max_length': 160}
    if failed:
        assert value['verification_error'] == {'category': 'protocol_error', 'reason': 'unsupported_version',
            'message': '目标站点版本尚未适配，请更新版本支持后重新验证', 'observed_version': 'v1.0.0-rc.99',
            'endpoint': '/api/status', 'method': 'GET'}
        assert 'secret-from-remote' not in str(value) and 'private-body' not in str(value)
    else:
        assert value['verification_error'] is None


@pytest.mark.parametrize('custom', ['true', 1, {}, None])
def test_template_options_are_typed_filtered_and_do_not_mutate_internal_data(login, db, public_site, custom):
    public_site.capabilities = {**public_site.capabilities, 'models': ['model-a', '', None, {}, True, '  ', 'model-a'],
        'groups': {'not': 'a list'}, 'custom_models': custom, 'verified_version': {'private': 'body'}}
    db.commit()
    before = deepcopy(public_site.capabilities)
    response = login('root').get('/api/sites')
    assert response.status_code == 200
    value = response.json()['items'][0]
    assert value['template_options'] == {'models': ['model-a'], 'groups': [], 'allow_custom_models': False,
                                         'routing_group_max_length': 160}
    assert value['verified_version'] is None
    db.expire_all()
    assert db.scalar(select(Site)).capabilities == before
