"""Pinned official/Colin channel contracts. All HTTP goes through MockTransport."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters import get_adapter, silicon, supports_adapter
from app.adapters.new_api import NewAPIAdapter
from app.adapters.newapi_compatibility import (
    RELEASES,
    REVIEWED_AT,
    WINDOW_END,
    WINDOW_START,
)
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.newapi_formats import get_format_specs
from app.routers.sites import SiteCreate, SitePatch
from app.security import encrypt


class RemoteFixture:
    def __init__(self, version):
        self.version = version
        self.flags = dict.fromkeys(('read', 'write', 'operate', 'sensitive_write'), True)
        self.identity = {'id': 71, 'role': 100, 'status': 1,
                         'permissions': {'admin_permissions': {'channel': self.flags}}}
        self.remote = {'id': 91, 'name': 'stable-name', 'type': 1, 'status': 2,
                       'models': 'catalog-model', 'group': 'default', 'settings': '{}', 'setting': '{}'}
        self.requests = []
        self.callback = lambda request: None
        self.write_response = None
        self.rows = []
        self.seller_groups = ['default']
        self.seller_models = ['catalog-model']

    @property
    def writes(self):
        return [r for r in self.requests if r.method not in ('GET', 'HEAD')]

    def handle(self, request):
        self.requests.append(request)
        self.callback(request)
        path = request.url.path
        if request.method != 'GET':
            if self.write_response is not None:
                return self.write_response
            if request.method == 'POST' and path == '/api/channel':
                return httpx.Response(307, headers={'Location': '/api/channel/'})
            if request.method == 'POST' and path in ('/api/channel/', '/api/seller/channel/'):
                self.remote = {'id': 91, **json.loads(request.content)['channel']}
                self.rows = [self.remote]
            return httpx.Response(200, json={'success': True, 'message': ''})
        if path == '/api/status':
            data = {'version': self.version}
        elif path == '/api/user/self':
            data = self.identity
        elif path == '/api/channel/models':
            data = [{'id': 'catalog-model'}]
        elif path == '/api/group/':
            data = ['default', 'alternate']
        elif path in ('/api/channel/', '/api/channel/search'):
            data = {'items': self.rows, 'total': len(self.rows)}
        elif path in ('/api/channel/91', '/api/seller/channel/91'):
            data = self.remote
        elif path == '/api/seller/channel/':
            data = {'items': self.rows, 'total': len(self.rows), 'can_write': True,
                    'can_toggle': self.flags['operate'], 'can_edit_routing': self.flags['write']}
        elif path == '/api/seller/channel/meta':
            data = {'models': [{'id': model} for model in self.seller_models], 'groups': self.seller_groups}
        else:
            pytest.fail(f'Unexpected mocked endpoint {path}')
        return httpx.Response(200, json={'success': True, 'data': data})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live requests forbidden in adapter contract tests')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)


@pytest.fixture(params=[NewAPIAdapter.STABLE_VERSION, 'v1.0.0-rc.35'])
def native(request):
    server = RemoteFixture(request.param)
    site = SimpleNamespace(adapter='new-api-v1', base_url='https://fixture.example', seller_user_id='71',
                           token_encrypted=encrypt('fixture-management-pat'), capabilities={})
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        adapter = NewAPIAdapter(site, transport=client.request)
        yield SimpleNamespace(adapter=adapter, site=site, server=server, client=client)


def create(native, **kwargs):
    default_models = ['catalog-model']
    config = kwargs.get('config')
    schema = config.get('credential_format') if isinstance(config, dict) else None
    if isinstance(schema, dict) and schema.get('type') == 'vertex_claude':
        default_models = ['claude-sonnet-5']
        native.server.seller_models = ['catalog-model', *default_models]
    return native.adapter.create(name='stable-name', key=kwargs.pop('key', 'fixture-key'),
                                 models=kwargs.pop('models', default_models), **kwargs)


@pytest.mark.parametrize('changed', [False, True])
def test_management_delete_checks_confirmed_remote_identity(native, changed):
    expected = {'id': '91', 'name': 'stable-name', 'type': 1}
    if changed:
        native.server.remote['type'] = 14
        with pytest.raises(RemoteError) as caught:
            native.adapter.delete('91', expected=expected)
        assert caught.value.unknown and caught.value.category == 'identity_mismatch'
        assert not native.server.writes
    else:
        native.adapter.delete('91', expected=expected)
        assert len(native.server.writes) == 1 and native.server.writes[0].method == 'DELETE'


def config_for(spec):
    number = spec['remote_type']
    if spec['schema_config']['type'] in ('azure_gpt', 'azure_claude'):
        from app.azure_credentials import azure_credential_fields
        fields = azure_credential_fields(key_for(spec), spec['schema_config']['type'])
        return {'credential_format': spec['schema_config'], **fields['channel_config']}
    return {'credential_format': spec['schema_config'],
            'base_url': ('https://acme-proxy.api.aws' if spec['schema_config']['type'] == 'aws_claude'
                         else 'https://relay.example' if number in (3, 8, 59, 60) else ''),
            'other': '{"default":"us-central1"}' if number == 41 else 'provider-id' if number in (18, 39, 49) else ''}


def key_for(spec):
    return {
        'aws_ak_sk': 'AK|SK|us-east-1', 'aws_api_key': 'BEARER|us-east-1', 'aws_bedrock': 'AK|SK|us-east-1',
        'azure_gpt': 'fixture-resource|fixture-key|2024-10-21', 'azure_claude': 'fixture-resource|fixture-key',
        'baidu_pair': 'AK|SK', 'zhipu_pair': 'ID.Secret', 'xunfei_triple': 'ID|SECRET|KEY',
        'tencent_triple': 'ID|SECRETID|KEY', 'kling_pair': 'AK|SK', 'jimeng_pair': 'AK|SK',
        'vertex_json': '{"project_id":"proj","client_email":"svc@example.com","private_key":"fake-pem"}',
        'vertex_gemini': '{"project_id":"proj","client_email":"svc@example.com","private_key":"fake-pem"}',
        'vertex_claude': '{"project_id":"proj","client_email":"svc@example.com","private_key":"fake-pem"}',
        'codex_json': '{"access_token":"token","account_id":"account","refresh_token":"refresh"}',
    }.get(spec['schema_config']['type'], 'fixture-key')


def test_factory_and_site_models_accept_explicit_native_adapter(native):
    assert supports_adapter('new-api-v1')
    assert isinstance(get_adapter(native.site), NewAPIAdapter)
    assert SitePatch(adapter='new-api-v1').adapter == 'new-api-v1'
    assert SiteCreate(name='Fixture', prefix='NAPI', base_url='https://fixture.example', adapter='new-api-v1',
                      seller_user_id='71', token='fixture-token').adapter == 'new-api-v1'


def test_verify_version_capabilities_and_management_headers(native):
    outcome = native.adapter.verify()
    cap = outcome['capabilities']
    assert outcome['verified_version'] == native.server.version
    assert cap['proxy'] == cap['channel_config'] == 'supported'
    assert cap['rpm'] == cap['tpm'] == 'unsupported' and cap['account_info'] == 'local_only'
    assert cap['custom_models'] is True
    assert (60 in cap['channel_types']) == (native.server.version != NewAPIAdapter.STABLE_VERSION)
    assert all(r.headers['Authorization'] == 'Bearer fixture-management-pat' for r in native.server.requests)
    assert all(r.headers['New-Api-User'] == '71' for r in native.server.requests)
    assert all('scope' not in r.url.params for r in native.server.requests if r.url.path == '/api/group/')
    assert not native.server.writes


def test_official_create_supports_all_selected_groups_and_rejects_any_unavailable_group(native):
    create(native, group='default,alternate', models=['provider-new-model'])
    assert json.loads(native.server.writes[0].content)['channel']['group'] == 'default,alternate'
    native.server.requests.clear()
    with pytest.raises(RemoteError, match='分组'):
        create(native, group='default,unavailable')
    assert not native.server.writes


@pytest.mark.parametrize('version', sorted(NewAPIAdapter.RBAC_VERSIONS))
def test_recent_rcs_use_explicit_permissions(native, version):
    native.server.version = version
    native.server.identity.pop('permissions')
    with pytest.raises(RemoteError, match='明确'):
        native.adapter.verify()
    assert not native.server.writes


@pytest.mark.parametrize('version', ['v0.13.1', 'v1.0.0-rc.10', 'v1.0.0-rc.36', 'v1.0.0-rc.35-custom', '', None, []])
def test_unreviewed_versions_block_before_write(native, version):
    native.server.version = version
    with pytest.raises(RemoteError) as caught:
        create(native)
    assert caught.value.category == 'protocol_error' and not native.server.writes


def test_official_release_manifest_covers_the_reviewed_three_month_window():
    expected = {f'v1.0.0-rc.{number}' for number in range(11, 36)} | {'v1.0.0-rc.19-i18nfix.2'}
    assert NewAPIAdapter.RC_VERSIONS == expected
    assert NewAPIAdapter.VERSIONS == expected | {'v0.13.2'}
    assert WINDOW_START == '2026-06-08' and WINDOW_END == REVIEWED_AT == '2026-09-08'
    assert all(WINDOW_START <= RELEASES[version].published_at[:10] <= WINDOW_END for version in expected)
    assert NewAPIAdapter.LEGACY_VERSIONS == {'v0.13.2', *(f'v1.0.0-rc.{n}' for n in range(11, 16))}
    assert RELEASES['v1.0.0-rc.35'].published_at == '2026-09-07T14:45:50Z'


@pytest.mark.parametrize('version', sorted(NewAPIAdapter.LEGACY_VERSIONS))
def test_reviewed_role_contract_does_not_require_newer_permission_dto(native, version):
    native.server.version = version
    native.server.identity.pop('permissions')
    native.server.identity['role'] = 10
    assert native.adapter.verify()['capabilities']['can_write'] is True
    native.adapter.toggle('91', False)
    request, = native.server.writes
    assert request.method == 'PUT' and request.url.path == '/api/channel/'
    assert json.loads(request.content) == {'id': 91, 'status': 2}


@pytest.mark.parametrize('version', sorted(NewAPIAdapter.VERSIONS))
def test_every_reviewed_release_create_readback_edit_and_status_contract(native, version):
    native.server.version = version
    if version in NewAPIAdapter.LEGACY_VERSIONS:
        native.server.identity.pop('permissions')
    capabilities = native.adapter.verify()['capabilities']
    native.site.capabilities = capabilities
    assert capabilities['compatibility_reviewed_at'] == '2026-09-08'
    assert capabilities['version_contract'] == ('role_admin' if version in NewAPIAdapter.LEGACY_VERSIONS else 'channel_rbac')

    # Different settings payloads share the same management DTO across tags.
    codes = {'api_key-v1', 'newapi-3-api-key-v1', 'newapi-33-aws-ak-sk-v1',
             'newapi-33-aws-api-key-v1', 'newapi-41-vertex-json-v1', 'newapi-14-aws-claude-v1'}
    for spec in (item for item in get_format_specs() if item['code'] in codes):
        cfg = config_for(spec)
        create(native, channel_type=spec['remote_type'], key=key_for(spec), config=cfg)
        request = native.server.writes[-1]
        assert request.method == 'POST' and request.url.path == '/api/channel/'
        payload = json.loads(request.content)
        assert set(payload) == {'mode', 'channel'} and payload['mode'] == 'single'
        assert payload['channel']['models'] == 'catalog-model'
        assert payload['channel']['group'] == 'default'
        assert isinstance(payload['channel']['setting'], str) and isinstance(payload['channel']['settings'], str)
        native.adapter.validate_created_config(native.server.remote, cfg, '', spec['remote_type'])
        assert native.adapter.find_unique_name('stable-name')['id'] == 91

    native.adapter.edit(91, changes={'remark': 'reviewed-contract'})
    edit = native.server.writes[-1]
    assert edit.method == 'PUT' and edit.url.path == '/api/channel/'
    assert json.loads(edit.content)['remark'] == 'reviewed-contract'
    assert 'key' not in json.loads(edit.content)
    native.adapter.toggle('91', True)
    toggle = native.server.writes[-1]
    if version in NewAPIAdapter.LEGACY_VERSIONS:
        assert toggle.method == 'PUT' and toggle.url.path == '/api/channel/'
        assert json.loads(toggle.content) == {'id': 91, 'status': 1}
    else:
        assert toggle.method == 'POST' and toggle.url.path == '/api/channel/91/status'
        assert json.loads(toggle.content) == {'status': 1}
    native.adapter.delete('91')
    assert native.server.writes[-1].method == 'DELETE'
    assert native.server.writes[-1].url.path == '/api/channel/91'


@pytest.mark.parametrize(('version', 'allowed_new_types'), [
    ('v0.13.2', set()), ('v1.0.0-rc.11', set()), ('v1.0.0-rc.15', set()),
    ('v1.0.0-rc.16', set()), ('v1.0.0-rc.21', set()),
    ('v1.0.0-rc.22', {59}), ('v1.0.0-rc.23', {59, 60}), ('v1.0.0-rc.35', {59, 60}),
])
def test_channel_type_capabilities_follow_each_releases_actual_catalog(native, version, allowed_new_types):
    native.server.version = version
    capabilities = native.adapter.verify()['capabilities']
    assert set(capabilities['channel_types']) & {59, 60} == allowed_new_types
    assert not {58, 61} & set(capabilities['channel_types'])  # No local format contract for these types.
    for number in {59, 60} - allowed_new_types:
        spec = next(item for item in get_format_specs() if item['remote_type'] == number)
        with pytest.raises(RemoteError, match='类型'):
            create(native, channel_type=number, key=key_for(spec), config=config_for(spec))
    assert not native.server.writes


def test_version_change_during_operation_prevents_wrong_contract_write(native):
    original = native.server.version
    native.server.callback = lambda r: setattr(native.server, 'version', 'unreviewed') if r.url.path == '/api/group/' else None
    with pytest.raises(RemoteError):
        create(native)
    assert original != native.server.version and not native.server.writes


@pytest.mark.parametrize('field,value', [('id', 72), ('id', '71'), ('status', 2), ('role', 5), ('role', '100')])
def test_identity_and_admin_role_are_checked_before_write(native, field, value):
    native.server.identity[field] = value
    with pytest.raises(RemoteError):
        create(native)
    assert not native.server.writes


@pytest.mark.parametrize('spec', get_format_specs(), ids=lambda spec: spec['code'])
def test_all_advertised_fixed_formats_create_correct_type_and_roundtrip(native, spec):
    cfg = config_for(spec)
    if native.server.version == NewAPIAdapter.STABLE_VERSION and spec['remote_type'] > 57:
        with pytest.raises(RemoteError, match='类型'):
            create(native, channel_type=spec['remote_type'], key=key_for(spec), config=cfg)
        assert not native.server.writes
        return
    native.site.capabilities = native.adapter.verify()['capabilities']
    assert create(native, channel_type=spec['remote_type'], key=key_for(spec), config=cfg) is None
    body = json.loads(native.server.writes[0].content)
    assert native.server.writes[0].url.path == '/api/channel/'
    assert body['mode'] == 'single' and body['channel']['type'] == spec['remote_type']
    if spec['schema_config']['type'] in ('azure_gpt', 'azure_claude'):
        assert body['channel']['key'] == 'fixture-key' and body['channel']['base_url'] == cfg['base_url']
    assert set(body) == {'mode', 'channel'}
    assert isinstance(body['channel']['settings'], str) and isinstance(body['channel']['setting'], str)
    assert not {'id', 'created_by', 'balance', 'account_info'} & body['channel'].keys()
    native.adapter.validate_created_config(native.server.remote, cfg, '', spec['remote_type'])
    assert native.adapter.find_unique_name('stable-name')['id'] == 91


@pytest.mark.parametrize('cfg', [
    {'credential_format': {'type': 'aws_api_key', 'remote_type': 33}}, {'settings': {'proxy': 'bad'}},
    {'created_by': 999}, {'balance': 999}, {'param_override': '{}'}, {'rpm_enabled': True, 'rpm_limit': 12},
])
def test_untrusted_config_cannot_inject_or_enable_unsupported_fields(native, cfg):
    with pytest.raises(RemoteError):
        create(native, config=cfg)
    assert not native.server.writes


def test_native_proxy_model_mappings_and_local_account_info(native):
    cfg = {'status': 1, 'base_url': 'https://relay.example', 'organization': 'organization-id',
           'priority': 12, 'weight': 5, 'auto_ban': 0,
           'model_mapping': {'public': 'private'}, 'status_code_mapping': {'429': '503'},
           'account_info': {'balance_usd': 20, 'rpm': 30, 'tpm': 100, 'prepaid': True, 'kd': False}}
    proxy = 'socks5://user:fixture-password@proxy.example:1080'
    native.site.capabilities = native.adapter.verify()['capabilities']
    create(native, config=cfg, proxy=proxy, models=['custom-model'])
    payload = json.loads(native.server.writes[0].content)['channel']
    assert payload['openai_organization'] == 'organization-id'
    assert json.loads(payload['setting']) == {'proxy': proxy}
    assert json.loads(payload['model_mapping']) == {'public': 'private'}
    assert payload['status'] == 1 and payload['auto_ban'] == 0 and payload['priority'] == 12
    assert 'account_info' not in payload and 'balance_usd' not in native.server.writes[0].content.decode()
    native.adapter.validate_created_config(native.server.remote, cfg, proxy, 1)


@pytest.mark.parametrize('field,value', [('status', 1), ('type', 14), ('base_url', 'https://wrong.example'),
                                        ('priority', 30), ('setting', '{}'), ('settings', '{}')])
def test_silently_dropped_or_changed_config_is_unknown_and_hides_proxy(native, field, value):
    cfg = {'credential_format': {'type': 'aws_api_key', 'remote_type': 33}, 'priority': 9}
    proxy = 'http://user:do-not-leak@proxy.example:3128'
    native.site.capabilities = native.adapter.verify()['capabilities']
    create(native, key='AK|us-east-1', channel_type=33, config=cfg, proxy=proxy)
    native.server.remote[field] = value
    with pytest.raises(RemoteError) as caught:
        native.adapter.validate_created_config(native.server.remote, cfg, proxy, 33)
    assert caught.value.unknown and 'do-not-leak' not in str(caught.value)


def test_toggle_uses_version_specific_route_and_only_status(native):
    native.adapter.toggle('91', True)
    request, = native.server.writes
    if native.server.version == NewAPIAdapter.STABLE_VERSION:
        assert request.method == 'PUT' and request.url.path == '/api/channel/'
        assert json.loads(request.content) == {'id': 91, 'status': 1}
    else:
        assert request.method == 'POST' and request.url.path == '/api/channel/91/status'
        assert json.loads(request.content) == {'status': 1}


def test_rc_create_enabled_requires_operate_permission(native):
    native.server.version = 'v1.0.0-rc.35'
    native.server.flags['operate'] = False
    with pytest.raises(RemoteError, match='启停'):
        create(native, config={'status': 1})
    assert not native.server.writes
    create(native, config={'status': 2})
    assert len(native.server.writes) == 1


def test_native_edit_preserves_type_settings_and_never_replays_masked_key(native):
    native.server.remote.update(type=41, other='{"default":"us-central1"}', key='MASKED',
                                settings='{"vertex_key_type":"api_key"}')
    native.adapter.edit(91, changes={'remark': 'changed'}, expected={'type': 41})
    body = json.loads(native.server.writes[0].content)
    assert body['type'] == 41 and json.loads(body['settings']) == {'vertex_key_type': 'api_key'}
    assert not {'key', 'status', 'created_by'} & body.keys()
    assert 'MASKED' not in native.server.writes[0].content.decode()


def test_before_write_failure_prevents_network_mutation(native):
    def reject():
        raise RemoteError('local permission revoked')
    native.adapter.before_write = reject
    with pytest.raises(RemoteError, match='revoked'):
        create(native)
    assert not native.server.writes


@pytest.mark.parametrize('response', [httpx.Response(500, text='fixture-key'), httpx.Response(200, text='not-json'),
                                      httpx.Response(200, json={}), httpx.Response(200, json=[]),
                                      httpx.Response(200, json={'success': 1, 'key': 'fixture-key'})])
def test_ambiguous_write_failure_never_leaks_response_or_reposts(native, response):
    native.server.write_response = response
    with pytest.raises(RemoteError) as caught:
        create(native)
    assert caught.value.unknown and 'fixture-key' not in str(caught.value)
    assert len(native.server.writes) == 1


def test_explicit_business_rejection_is_known_failure_and_sanitized(native):
    native.server.write_response = httpx.Response(200, json={'success': False, 'message': 'fixture-key'})
    with pytest.raises(RemoteError) as caught:
        create(native)
    assert not caught.value.unknown and caught.value.category == 'business_error'
    assert 'fixture-key' not in str(caught.value) and len(native.server.writes) == 1


def test_colin_extension_serializes_rpm_and_aws_without_account_info(native):
    native.server.version = TcpRedAdapter.VERSION
    native.site.adapter = 'tcp-red-v1'
    native.adapter = TcpRedAdapter(native.site, transport=native.client.request)
    native.site.capabilities = native.adapter.verify()['capabilities']
    cfg = {'credential_format': {'type': 'aws_ak_sk', 'remote_type': 33}, 'rpm_enabled': True, 'rpm_limit': 45,
           'account_info': {'rpm': 300, 'tpm': 10000, 'balance_usd': 5}}
    create(native, channel_type=33, key='AK|SK|us-east-1', config=cfg, proxy='http://proxy.example:3128')
    payload = json.loads(native.server.writes[0].content)['channel']
    assert json.loads(payload['setting']) == {'rate_limit_enabled': True, 'rpm_limit': 45, 'proxy': 'http://proxy.example:3128'}
    assert json.loads(payload['settings']) == {'aws_key_type': 'ak_sk'}
    assert 'tpm_limit' not in payload['setting'] and 'balance' not in payload
    native.adapter.validate_created_config(native.server.remote, cfg, 'http://proxy.example:3128', 33)
    dropped = deepcopy(native.server.remote)
    dropped['setting'] = '{"proxy":"http://proxy.example:3128"}'
    with pytest.raises(RemoteError) as caught:
        native.adapter.validate_created_config(dropped, cfg, 'http://proxy.example:3128', 33)
    assert caught.value.unknown


def test_silicon_retains_seller_object_settings_and_separate_version_contract(native):
    native.server.version = SiliconAdapter.VERSION
    native.adapter = SiliconAdapter(native.site, transport=native.client.request)
    native.site.capabilities = native.adapter.verify()['capabilities']
    assert native.site.capabilities['channel_types'] == [1, 3, 14, 20, 24, 33, 41]
    create(native, config={'base_url': 'https://relay.example', 'status': 2})
    body = json.loads(native.server.writes[0].content)['channel']
    assert isinstance(body['settings'], dict) and body['base_url'] == 'https://relay.example'
    native.server.requests.clear()
    with pytest.raises(RemoteError):
        create(native, channel_type=60)
    assert not native.server.writes
    create(native, proxy='http://proxy.example:3128')
    body = json.loads(native.server.writes[0].content)['channel']
    assert body['proxy'] == 'http://proxy.example:3128' and 'setting' not in body


@pytest.fixture(params=sorted(SiliconAdapter.VERSIONS))
def seller(request):
    server = RemoteFixture(request.param)
    server.identity = {'id': 71, 'status': 1, 'role': 5}
    server.flags['write'] = False  # Seller routing rights, not administrator rights.
    site = SimpleNamespace(adapter='silicon-v1', base_url='https://fixture.example', seller_user_id='71',
                           token_encrypted=encrypt('fixture-management-pat'), capabilities={})
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        yield SimpleNamespace(adapter=SiliconAdapter(site, transport=client.request), site=site,
                              server=server, client=client)


def test_seller_create_and_model_edit_validate_every_selected_group(seller):
    seller.server.seller_groups = ['default', 'alternate']
    create(seller, group='default,alternate')
    assert json.loads(seller.server.writes[0].content)['channel']['group'] == 'default,alternate'
    seller.adapter.edit('91', changes={'models': 'catalog-model'})
    assert json.loads(seller.server.writes[-1].content)['group'] == 'default,alternate'
    seller.server.requests.clear()
    seller.server.seller_groups = ['default']
    with pytest.raises(RemoteError, match='路由组'):
        seller.adapter.edit('91', changes={'models': 'catalog-model'})
    with pytest.raises(RemoteError, match='路由组'):
        create(seller, group='default,alternate')
    assert not seller.server.writes


def test_seller_custom_model_contract_is_version_specific(seller):
    outcome = seller.adapter.verify()
    modern = seller.server.version in {'v1.0.0-rc.25-fix-36', 'v1.0.0-rc.25-fix-38'}
    assert outcome['capabilities']['custom_models'] is modern
    if modern:
        assert outcome['capabilities']['model_max_bytes'] == 255
        create(seller, models=['provider-new-model'])
        assert json.loads(seller.server.writes[0].content)['channel']['models'] == 'provider-new-model'
    else:
        with pytest.raises(RemoteError, match='模型'):
            create(seller, models=['provider-new-model'])
        assert not seller.server.writes


@pytest.mark.parametrize('models', [[], [''], [' '], [' trailing '], ['x,y'], ['x\ny'], ['x\ry'],
                                   ['x' * 256], ['模' * 86], [None], 'model', ['model'] * 201])
def test_seller_custom_models_still_validate_wire_syntax(seller, models):
    with pytest.raises(RemoteError, match='模型'):
        create(seller, models=models)
    assert not seller.server.writes


@pytest.mark.parametrize('spec', [s for s in get_format_specs() if s['remote_type'] in SiliconAdapter.CHANNEL_TYPES],
                         ids=lambda spec: spec['code'])
def test_seller_versions_accept_catalog_types_and_preserve_wire_settings(seller, spec):
    cfg = config_for(spec)
    outcome = seller.adapter.verify()
    seller.site.capabilities = outcome['capabilities']
    assert outcome['verified_version'] == seller.server.version
    assert spec['code'] in outcome['capabilities']['formats']
    assert outcome['capabilities']['can_edit_routing'] is False
    create(seller, channel_type=spec['remote_type'], key=key_for(spec), config=cfg)
    body = json.loads(seller.server.writes[0].content)['channel']
    assert body['type'] == spec['remote_type']
    if spec['schema_config']['type'] in ('azure_gpt', 'azure_claude'):
        assert body['key'] == 'fixture-key' and body['base_url'] == cfg['base_url']
    assert isinstance(body['settings'], dict)
    assert not {'priority', 'weight', 'created_by', 'seller_id'} & body.keys()
    # Reads from fix-36 return stored settings as JSON text, unlike write DTOs.
    remote = {**seller.server.remote, 'settings': json.dumps(body['settings'])}
    seller.adapter.validate_created_config(remote, cfg, '', spec['remote_type'])
    seller.server.remote = remote
    seller.adapter.edit('91', changes={'remark': 'Updated'})
    edited = json.loads(seller.server.writes[-1].content)
    assert edited['settings'] == body['settings']
    assert not {'key', 'status', 'priority', 'weight'} & edited.keys()
    assert all(not r.url.path.startswith('/api/channel') for r in seller.server.requests)


@pytest.mark.parametrize('version', ['v1.0.0-rc.35', 'v1.0.0-rc.25-fix-999', None, 36])
def test_seller_unknown_versions_with_compatible_interfaces_use_seller_payload(seller, version):
    seller.server.version = version
    create(seller)
    assert len(seller.server.writes) == 1
    assert seller.server.writes[0].url.path == '/api/seller/channel/'
    payload = json.loads(seller.server.writes[0].content)
    assert payload['mode'] == 'single' and isinstance(payload['channel']['settings'], dict)
    assert 'setting' not in payload['channel']


def test_seller_contract_drift_blocks_before_durable_attempt(seller):
    writes = []
    seller.adapter.before_write = lambda: writes.append(True)
    other = (SiliconAdapter.VERSION if seller.server.version == SiliconAdapter.LEGACY_VERSION
             else SiliconAdapter.LEGACY_VERSION)
    def change_version(request):
        if request.url.path.endswith('/meta'):
            seller.server.version = other
    seller.server.callback = change_version
    with pytest.raises(RemoteError, match='协议发生变化'):
        create(seller)
    assert not writes and not seller.server.writes


@pytest.mark.parametrize('extra', ['[]', 'null', 'not-json', [], True])
def test_seller_edit_rejects_invalid_settings_without_overwriting(seller, extra):
    seller.server.remote['settings'] = extra
    with pytest.raises(RemoteError, match='settings'):
        seller.adapter.edit('91', changes={'remark': 'Changed'})
    assert not seller.server.writes


def test_current_seller_proxy_and_readback_use_public_seller_dto(seller):
    seller.server.version = SiliconAdapter.VERSION
    cfg = {'credential_format': {'type': 'aws_api_key', 'remote_type': 33},
           'model_mapping': {'catalog-model': 'actual-model'}}
    proxy = 'socks5://user:fixture-private@proxy.example:1080'
    outcome = seller.adapter.verify()
    seller.site.capabilities = {**outcome['capabilities'], 'verified_version': outcome['verified_version']}
    assert outcome['capabilities']['proxy'] == 'supported'
    assert outcome['capabilities']['rpm'] == 'unsupported'
    create(seller, key='BEARER|us-east-1', channel_type=33, config=cfg, proxy=proxy)
    body = json.loads(seller.server.writes[-1].content)['channel']
    assert body['proxy'] == proxy and body['settings'] == {'aws_key_type': 'api_key'}
    assert not {'setting', 'auto_ban', 'status_code_mapping', 'param_override', 'header_override', 'tag'} & body.keys()

    # Actual fix-36 reads serialize settings and omit all administrator-only fields.
    remote = {**seller.server.remote, 'settings': json.dumps(body['settings'])}
    # Reconciliation uses a fresh adapter loaded from the stored capability version.
    fresh = SiliconAdapter(seller.site, transport=seller.client.request)
    fresh.validate_created_config(remote, cfg, proxy, 33)
    seller.server.remote = remote
    fresh.edit('91', changes={'remark': 'preserve proxy'}, key='REPLACEMENT|us-east-1')
    edited = json.loads(seller.server.writes[-1].content)
    assert edited['proxy'] == proxy and edited['settings'] == {'aws_key_type': 'api_key'}
    assert edited['key'] == 'REPLACEMENT|us-east-1'
    assert not {'setting', 'auto_ban', 'status_code_mapping', 'status', 'priority', 'weight', 'tag'} & edited.keys()


@pytest.mark.parametrize('remote_proxy', ['', None, {}, 'http://wrong.example:3128'])
def test_current_seller_dropped_or_changed_proxy_requires_review(seller, remote_proxy):
    seller.server.version = SiliconAdapter.VERSION
    proxy = 'http://user:fixture-private@proxy.example:3128'
    create(seller, proxy=proxy)
    remote = {**seller.server.remote, 'proxy': remote_proxy}
    with pytest.raises(RemoteError) as caught:
        seller.adapter.validate_created_config(remote, None, proxy, 1)
    assert caught.value.unknown and caught.value.category == 'configuration_mismatch'
    assert 'fixture-private' not in str(caught.value)


def test_current_seller_unrequested_remote_proxy_is_not_silently_accepted(seller):
    seller.server.version = SiliconAdapter.VERSION
    create(seller)
    remote = {**seller.server.remote, 'proxy': 'http://unexpected.example:3128'}
    with pytest.raises(RemoteError) as caught:
        seller.adapter.validate_created_config(remote, None, '', 1)
    assert caught.value.unknown


def test_documented_seller_retains_setting_payload_and_rejects_proxy(seller):
    seller.server.version = 'v1.0.0-rc.25-fix-22-multiseller-2'
    cfg = {'auto_ban': 0, 'status_code_mapping': {'429': '503'}}
    create(seller, config=cfg)
    body = json.loads(seller.server.writes[-1].content)['channel']
    assert body['settings'] == {} and body['setting'] == '{}'
    assert body['auto_ban'] == 0 and json.loads(body['status_code_mapping']) == {'429': '503'}
    assert 'proxy' not in body
    seller.adapter.validate_created_config(seller.server.remote, cfg, '', 1)
    seller.server.requests.clear()
    with pytest.raises(RemoteError, match='代理'):
        create(seller, proxy='http://proxy.example:3128')
    assert not seller.server.writes


@pytest.mark.parametrize('config', [
    {'auto_ban': 0}, {'status_code_mapping': {'429': '503'}},
    {'rpm_enabled': True, 'rpm_limit': 45}, {'param_override': '{}'},
    {'settings': {'aws_key_type': 'api_key'}}, {'priority': 1}, {'weight': 2},
])
def test_current_seller_forbidden_config_never_reaches_remote_write(seller, config):
    seller.server.version = SiliconAdapter.VERSION
    durable_attempts = []
    seller.adapter.before_write = lambda: durable_attempts.append(True)
    with pytest.raises(RemoteError):
        create(seller, config=config)
    assert not seller.server.writes and not durable_attempts


def test_current_seller_edit_preserves_only_public_settings(seller):
    seller.server.version = SiliconAdapter.VERSION
    seller.server.remote.update(type=20, settings=json.dumps({'openrouter_enterprise': True,
                                 'unknown_server_setting': 'do-not-replay'}), proxy='')
    seller.adapter.edit('91', changes={'remark': 'changed'})
    body = json.loads(seller.server.writes[-1].content)
    assert body['settings'] == {'openrouter_enterprise': True}
    assert 'do-not-replay' not in seller.server.writes[-1].content.decode()


def test_current_seller_normalizes_proxy_before_serializing(seller):
    seller.server.version = SiliconAdapter.VERSION
    create(seller, proxy='  http://proxy.example:3128  ')
    assert json.loads(seller.server.writes[-1].content)['channel']['proxy'] == 'http://proxy.example:3128'
    seller.adapter.validate_created_config(seller.server.remote, None, '  http://proxy.example:3128  ', 1)


@pytest.mark.parametrize('proxy', [None, {}, True])
def test_current_seller_unknown_proxy_shape_stops_edit(seller, proxy):
    seller.server.version = SiliconAdapter.VERSION
    seller.server.remote['proxy'] = proxy
    with pytest.raises(RemoteError, match='代理'):
        seller.adapter.edit('91', changes={'remark': 'changed'})
    assert not seller.server.writes


@pytest.mark.parametrize('config', [[], 'invalid', True])
def test_seller_malformed_config_is_safe_error(seller, config):
    with pytest.raises(RemoteError, match='配置必须是对象'):
        create(seller, config=config)
    assert not seller.server.writes
