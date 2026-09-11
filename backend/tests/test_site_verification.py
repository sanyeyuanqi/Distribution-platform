"""Verification errors stay actionable without turning unknown into unsupported."""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from app.adapters import silicon
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.db import uid, utcnow
from app.models import Site
from app.routers.sites import verify_site
from app.security import encrypt
from app.site_verification import safe_diagnostic, verification_state


@pytest.mark.parametrize(('version', 'observed'), [
    ('v1.0.0-rc.25-fix-41', 'v1.0.0-rc.25-fix-41'),
    ('v9.2.3-future', 'v9.2.3-future'),
    ('not-a-version secret-token', ''),
    (None, ''),
    (123, ''),
])
def test_version_is_diagnostic_and_compatible_interfaces_verify(monkeypatch, version, observed):
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, urlsplit(url).path))
        assert method == 'GET'
        if urlsplit(url).path == '/api/user/self':
            data = {'id': 7}
        elif urlsplit(url).path == '/api/status':
            data = {'version': version}
        elif urlsplit(url).path == '/api/seller/channel/':
            data = {'items': [], 'total': 0, 'can_write': True, 'can_toggle': True, 'can_edit_routing': False}
        else:
            assert urlsplit(url).path == '/api/seller/channel/meta'
            data = {'models': [], 'groups': ['default']}
        return httpx.Response(200, json={'success': True, 'data': data})
    monkeypatch.setattr(silicon, 'safe_request', transport)
    site = Site(name='Fixture', prefix='VERIFY', base_url='https://fixture.example', seller_user_id='7',
                token_encrypted=encrypt('secret-token'), enabled=True, verified_at=utcnow(),
                capabilities={'create': 'supported'}, health='healthy')
    assert verify_site(site) is True
    assert site.capabilities['protocol_contract'] == SiliconAdapter.COMPACT_CONTRACT
    assert site.capabilities['verified_version'] == observed
    assert 'secret-token' not in str(site.capabilities)
    assert site.verified_at is not None and site.enabled is True
    assert site.health == 'healthy'
    assert calls == [('GET', '/api/user/self'), ('GET', '/api/status'),
                     ('GET', '/api/seller/channel/'), ('GET', '/api/seller/channel/meta')]
    assert verification_state(site) == ('verified', None)


@pytest.mark.parametrize(('response', 'category'), [
    (httpx.Response(404, text='secret-body'), 'protocol_error'),
    (httpx.Response(403, json={'success': False, 'message': 'secret-body'}), 'permission_denied'),
    (httpx.Response(200, text='secret-body'), 'protocol_error'),
    (httpx.Response(200, json={'data': {'id': 7}}), 'protocol_error'),
])
def test_silicon_verification_transport_errors_include_endpoint_without_body(monkeypatch, response, category):
    monkeypatch.setattr(silicon, 'safe_request', lambda *args, **kwargs: response)
    site = Site(name='Fixture', prefix='VERIFY', base_url='https://fixture.example', seller_user_id='7',
                token_encrypted=encrypt('secret-token'), enabled=True)
    assert verify_site(site) is False
    error = site.capabilities['verification_error']
    assert error['category'] == category and error['endpoint'] == '/api/user/self'
    assert 'secret' not in str(error)


def test_diagnostics_reject_untrusted_free_text_and_stale_healthy_status():
    error = safe_diagnostic('unknown-secret', reason='secret-reason',
        observed_version='v1.0.0?secret-token', endpoint='/api/status?token=secret', method='GET')
    assert error == {'category': 'remote_error', 'message': '目标站点验证失败，请检查接入配置'}
    site = SimpleNamespace(capabilities={'verification_error': error}, verified_at=utcnow(), health='healthy')
    assert verification_state(site) == ('failed', error)
    site.capabilities = {}
    site.verified_at = None
    assert verification_state(site) == ('unverified', None)
    site.verified_at = utcnow()
    assert verification_state(site) == ('verified', None)


def test_silicon_version_contract_remains_explicit():
    assert 'v1.0.0-rc.25-fix-999' not in SiliconAdapter.VERSIONS


class SellerFixture:
    def __init__(self, version, *, change_before_write=False, can_write=True):
        self.version = version
        self.change_before_write = change_before_write
        self.can_write = can_write
        self.status_reads = 0
        self.calls, self.writes, self.events = [], [], []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.site = Site(name='Fixture', prefix='FIX38', base_url='https://mock.example', seller_user_id='7',
            token_encrypted=encrypt('management-token'), enabled=True)
        self.adapter = SiliconAdapter(self.site, transport=self.client.request,
            before_write=lambda: self.events.append('before_write'))

    def handle(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path == '/api/status':
            self.status_reads += 1
            version = (SiliconAdapter.LEGACY_VERSION if self.change_before_write and self.status_reads > 1 else self.version)
            data = {'version': version}
        elif request.url.path == '/api/user/self':
            data = {'id': 7, 'role': 5}
        elif request.url.path == '/api/seller/channel/meta':
            data = {'models': [{'id': 'known-model'}], 'groups': ['default', 'azure', 'az-claude'],
                    'prefill_groups': ['default']}
        elif request.url.path == '/api/seller/channel/' and request.method == 'GET':
            data = {'can_write': self.can_write, 'can_toggle': True, 'can_edit_routing': False,
                    'allowed_groups': ['default', 'azure', 'az-claude'], 'items': [], 'total': 0,
                    'seller_status': 1, 'type_counts': {}}
        elif request.url.path == '/api/seller/channel/' and request.method == 'POST':
            self.writes.append(json.loads(request.content))
            self.events.append('POST')
            data = {'id': 17}
        else:
            pytest.fail(f'Unexpected mock request: {request.method} {request.url.path}')
        return httpx.Response(200, json={'success': True, 'data': data})


@pytest.mark.parametrize('version', sorted(SiliconAdapter.COMPACT_VERSIONS))
@pytest.mark.parametrize('can_write', [False, True])
def test_reviewed_silicon_compact_versions_verify_real_capabilities(version, can_write):
    fixture = SellerFixture(version, can_write=can_write)
    with fixture.client:
        outcome = fixture.adapter.verify()
    cap = outcome['capabilities']
    assert outcome['verified_version'] == version
    assert cap['can_write'] is can_write and cap['can_toggle'] is True and cap['can_edit_routing'] is False
    assert cap['create'] == ('supported' if can_write else 'permission_denied')
    assert cap['test'] == ('supported' if can_write else 'permission_denied')
    assert cap['groups'] == ['default', 'azure', 'az-claude']
    assert cap['models'] == ['known-model'] and cap['custom_models'] is True
    assert cap['channel_types'] == [1, 3, 14, 20, 24, 33, 41]
    assert cap['proxy'] == cap['usage'] == 'supported'
    assert cap['proxy_encoding'] == 'top_level' and cap['settings_encoding'] == 'object'
    assert cap['unsupported_config_fields'] == ['auto_ban', 'status_code_mapping']
    assert fixture.writes == []


@pytest.mark.parametrize('channel_type', [1, 3, 14, 20, 24, 33, 41])
def test_fix38_creates_with_compact_dto_and_exact_protocol_fields(channel_type):
    fixture = SellerFixture('v1.0.0-rc.25-fix-38')
    config = {'status': 2}
    key = 'provider-key'
    if channel_type == 3:
        config.update(base_url='https://fixture-resource.openai.azure.com', other='2025-04-01-preview')
    if channel_type == 14:
        config.update(base_url='https://fixture-resource.services.ai.azure.com/anthropic')
    if channel_type == 41:
        config.update(other='{"default":"global"}', credential_format={'type': 'vertex_api_key', 'remote_type': 41})
    if channel_type == 33:
        config.update(credential_format={'type': 'aws_api_key', 'remote_type': 33})
        key = 'provider-key|us-east-1'
    with fixture.client:
        result = fixture.adapter.create(name='fixture', key=key, models=['custom-model'],
            group='default,azure', channel_type=channel_type, config=config, proxy='http://proxy.example:8080')
    assert result == '17' and fixture.events == ['before_write', 'POST']
    body = fixture.writes[0]
    assert body['mode'] == 'single'
    channel = body['channel']
    assert set(channel) <= SiliconAdapter.SELLER_FIELDS
    assert channel['models'] == 'custom-model' and channel['group'] == 'default,azure'
    assert channel['key'] == key and channel['type'] == channel_type
    assert channel['proxy'] == 'http://proxy.example:8080'
    assert isinstance(channel['settings'], dict)
    assert set(channel['settings']) <= SiliconAdapter.SELLER_SETTINGS
    assert 'setting' not in channel and 'auto_ban' not in channel and 'status_code_mapping' not in channel
    for name in ('base_url', 'other'):
        if name in config:
            assert channel[name] == config[name]


def test_fix38_version_change_or_permission_denied_never_attempts_write():
    for fixture in (SellerFixture('v1.0.0-rc.25-fix-38', change_before_write=True),
                    SellerFixture('v1.0.0-rc.25-fix-38', can_write=False)):
        with fixture.client, pytest.raises(RemoteError):
            fixture.adapter.create(name='fixture', key='provider-key', models=['known-model'])
        assert fixture.writes == [] and fixture.events == []


def test_fix38_rejects_ungranted_group_before_write():
    fixture = SellerFixture('v1.0.0-rc.25-fix-38')
    with fixture.client, pytest.raises(RemoteError, match='路由组'):
        fixture.adapter.create(name='fixture', key='provider-key', models=['known-model'], group='default,forbidden')
    assert fixture.writes == [] and fixture.events == []


@pytest.mark.parametrize('verification_succeeds', [False, True])
def test_enabling_site_reverifies_failure_despite_stale_success_fields(db, login, monkeypatch, verification_succeeds):
    from test_site_distribution import add_enabled_template
    fixture = SellerFixture('v1.0.0-rc.25-fix-38')
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, urlsplit(url).path))
        if verification_succeeds:
            return fixture.client.request(method, url, **kwargs)
        return httpx.Response(503, json={'success': False, 'message': 'private-upstream-detail'})
    monkeypatch.setattr(silicon, 'safe_request', transport)
    site = Site(id=uid(), name='Stale verification', prefix='STALE', base_url='https://mock.example',
                seller_user_id='7', token_encrypted=encrypt('management-token'), enabled=False,
                health='healthy', verified_at=utcnow(),
                capabilities={'verification_error': {'category': 'protocol_error'}})
    db.add(site)
    db.flush()
    add_enabled_template(db, site, models=['known-model'])
    db.commit()
    with fixture.client:
        response = login('root').patch('/api/sites/' + site.id, json={'enabled': True})
    assert response.status_code == 200, response.text
    assert response.json()['enabled'] is verification_succeeds
    assert calls and all(method == 'GET' for method, _ in calls)
    db.refresh(site)
    assert verification_state(site)[0] == ('verified' if verification_succeeds else 'failed')
    assert site.enabled is verification_succeeds
    if verification_succeeds:
        assert site.verified_at is not None and site.capabilities['verified_version'] == fixture.version
        assert 'verification_error' not in site.capabilities
    else:
        assert site.verified_at is None and site.capabilities['verification_error']['category'] == 'server_error'
    assert 'management-token' not in response.text and 'private-upstream-detail' not in response.text
