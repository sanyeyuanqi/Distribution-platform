"""SpaceX Hub contracts use public-UI fixtures and MockTransport, never live sites."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters import silicon, spacex_hub, spacex_hub_build
from app.adapters.newapi_builds import build_snapshot
from app.adapters.silicon import RemoteError
from app.adapters.spacex_hub import SpaceXHubAdapter
from app.channel_service import target_issues
from app.db import utcnow
from app.models import Site
from app.routers.sites import site_json
from app.security import encrypt


class HubFixture:
    def __init__(self):
        self.identity = {
            'id': 49, 'role': 5, 'status': 1,
            'admin_hub': {'id': 46, 'role': 'user', 'is_primary_account': 0, 'is_sub_root': False,
                          'is_top_root': False, 'supplier_id': 8, 'line_id': 21,
                          'visible_site_ids': [4], 'capabilities': {'channel_write': True}},
        }
        self.status = {'version': '', 'quota_per_unit': 500000}
        self.visible_sites = [{'id': 4, 'name': 'Fixture destination', 'status': 'online', 'api_protocol': 'new_api'}]
        self.platforms = {'platform_types': [
            {'platform_type_key': 'openai_gpt', 'platform_display_name': 'OpenAI',
             'new_api_type': 1, 'model_series': 'openai.gpt', 'key_fields': [], 'models': ['gpt-4o']},
        ]}
        self.publish_fields = {'site_id': 4, 'fields': [], 'site_defaults': {'create_status': 2,
            'encrypt_channel_name': False, 'site_rewrites_routing': False, 'default_priority': 5, 'default_discount': 1}}
        self.remote = {
            'id': 11, 'name': 'stable-hub-fixture', 'type': 1, 'platform_channel_type': 'openai_gpt',
            'model_series': 'openai.gpt', 'models': 'gpt-4o', 'group': 'default', 'status': 2, 'priority': 0,
            'supplier_id': 8, 'owner_user_id': 49, 'selected_site_ids': [4],
            'sites': [{'site_id': 4, 'remote_channel_id': 21, 'remote_status': 2,
                       'last_publish_status': 'created'}],
            'key': 'never-expose-remote-key', 'key_preview': 'masked',
        }
        self.publishing = {'11': {'has_active_tasks': False, 'sites': [
            {'site_id': 4, 'remote_channel_id': 21, 'publish_status': 'created', 'task_status': 'succeeded'},
        ]}}
        self.items = []
        self.created_data = {'id': 11}
        self.test_data = {'success': True, 'time': 0.125, 'message': 'never-expose-upstream-message'}
        self.usage_data = {'used_quota': 1250000}
        self.build = spacex_hub_build.BUILD_ID
        self.requests, self.events, self.build_checks = [], [], []
        self.overrides = {}
        self.on_request = lambda request: None

    @property
    def writes(self):
        # This exact POST endpoint is a read-only batch query in the public UI.
        return [request for request in self.requests
                if request.method not in ('GET', 'HEAD')
                and request.url.path != '/api/admin-hub/channels/publish-status']

    @property
    def expected(self):
        return {'id': '11', 'name': 'stable-hub-fixture', 'type': 1}

    def identify_build(self, base_url, status, transport):
        self.build_checks.append((base_url, deepcopy(status)))
        return self.build

    def handle(self, request):
        self.requests.append(request)
        self.events.append((request.method, request.url.path))
        self.on_request(request)
        assert request.url.host == httpx.URL(spacex_hub_build.ORIGIN).host
        assert not request.url.path.startswith('/api/channel'), 'Hub must never fall back to administrator routes'
        assert request.headers['Authorization'] == 'Bearer fixture-hub-management-token'
        assert request.headers['New-Api-User'] == '49'
        key = (request.method, request.url.path)
        if key in self.overrides:
            outcome = self.overrides[key]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if key == ('GET', '/api/status'):
            data = self.status
        elif key == ('GET', '/api/user/self'):
            data = self.identity
        elif key == ('GET', '/api/admin-hub/usage-logs/sites'):
            data = self.visible_sites
        elif key == ('GET', '/api/admin-hub/channels/platform-types'):
            data = self.platforms
        elif key == ('GET', '/api/admin-hub/channels/channel-types'):
            data = [{'value': 1, 'label': 'OpenAI'}]
        elif key == ('GET', '/api/admin-hub/channels/model-options'):
            data = {'platform_type': 'openai_gpt', 'new_api_type': 1, 'model_series': 'openai.gpt',
                    'items': [{'model_name': 'gpt-4o', 'has_price': True, 'in_whitelist': True}]}
        elif key == ('GET', '/api/admin-hub/channels/key-field-options'):
            data = {'fields': {}}
        elif key == ('GET', '/api/admin-hub/sites/4/groups'):
            data = {'groups': ['default']}
        elif key == ('GET', '/api/admin-hub/site-publish-fields/4'):
            data = self.publish_fields
        elif key == ('GET', '/api/admin-hub/channels'):
            data = {'items': self.items, 'total': len(self.items)}
        elif key == ('GET', '/api/admin-hub/channels/11'):
            data = self.remote
        elif key == ('POST', '/api/admin-hub/channels/publish-status'):
            assert json.loads(request.content) == {'ids': [11]}
            data = self.publishing
        elif key == ('POST', '/api/admin-hub/channels'):
            data = self.created_data
        elif key == ('POST', '/api/admin-hub/channels/11/status'):
            data = None
        elif key == ('POST', '/api/admin-hub/channels/11/test'):
            data = self.test_data
        elif key == ('GET', '/api/admin-hub/channels/11/realtime-used-quota'):
            assert dict(request.url.params) == {'site_id': '4'}
            data = self.usage_data
        else:
            pytest.fail(f'Unexpected fixture endpoint: {request.method} {request.url.path}')
        return httpx.Response(200, json={'success': True, 'data': deepcopy(data)})


@pytest.fixture(autouse=True)
def prohibit_live_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('SpaceX contract tests must never contact a real remote')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)
    monkeypatch.setattr(spacex_hub, 'safe_request', forbidden, raising=False)


@pytest.fixture
def hub(monkeypatch):
    server = HubFixture()
    site = SimpleNamespace(adapter='spacex-hub-v1', base_url=spacex_hub_build.ORIGIN,
                           seller_user_id='49', token_encrypted=encrypt('fixture-hub-management-token'),
                           routing_group='default', capabilities={})
    monkeypatch.setattr(spacex_hub, 'identify_build', server.identify_build)
    with httpx.Client(transport=httpx.MockTransport(server.handle), follow_redirects=False) as client:
        adapter = SpaceXHubAdapter(site, transport=client.request,
                                   before_write=lambda: server.events.append('before_write'))
        yield SimpleNamespace(server=server, site=site, client=client, adapter=adapter)


def create_fixture(adapter, **changes):
    values = {'name': 'stable-hub-fixture', 'key': 'fixture-upstream-key', 'models': ['gpt-4o'],
              'group': 'default', 'channel_type': 1, 'config': {'status': 2}, 'remark': 'fixture remark'}
    values.update(changes)
    return adapter.create(**values)


def test_user_subaccount_verifies_hub_contract_without_administrator_fallback(hub):
    result = hub.adapter.verify()
    assert result['identity'] == {'id': '49'}
    assert result['verified_version'] == spacex_hub_build.BUILD_ID
    cap = result['capabilities']
    assert cap['read'] == 'supported'
    assert cap['can_write'] is True and cap['create'] == 'supported'
    assert cap['multi_key'] == cap['delete'] == 'unsupported'
    assert hub.server.build_checks and not hub.server.writes


def test_supplier_can_verify_check_and_read_but_cannot_create(hub):
    hub.server.identity.update(role=8, admin_hub={
        'role': 'supplier', 'is_primary_account': 1, 'supplier_id': 8,
        'visible_site_ids': [4], 'capabilities': {'channel_write': True}})
    result = hub.adapter.verify()
    assert result['capabilities']['read'] == 'supported'
    assert result['capabilities']['create'] != 'supported'
    hub.adapter.check_connection()
    hub.adapter.channels()
    with pytest.raises(RemoteError) as caught:
        create_fixture(hub.adapter)
    assert caught.value.category == 'permission_denied'
    assert not hub.server.writes


@pytest.mark.parametrize('change', [
    {'role': '5'}, {'role': True}, {'id': '49'}, {'id': 50}, {'status': '1'}, {'status': 2},
    {'admin_hub': {'id': 46, 'role': 'user', 'is_primary_account': 0, 'supplier_id': '8'}},
])
def test_identity_and_role_are_strict_and_must_match_the_configured_account(hub, change):
    hub.server.identity.update(change)
    with pytest.raises(RemoteError):
        hub.adapter.verify()
    assert not hub.server.writes


@pytest.mark.parametrize('allowed', [False, None, 1, 'true'])
def test_channel_write_requires_explicit_boolean_permission(hub, allowed):
    hub.server.identity['admin_hub']['capabilities']['channel_write'] = allowed
    with pytest.raises(RemoteError) as caught:
        create_fixture(hub.adapter)
    assert caught.value.category == 'permission_denied'
    assert not hub.server.writes


def test_server_403_is_safe_and_never_triggers_a_fallback(hub):
    hub.server.overrides[('GET', '/api/admin-hub/channels')] = httpx.Response(
        403, json={'success': False, 'message': 'never-expose-remote-token-or-sql'})
    with pytest.raises(RemoteError) as caught:
        hub.adapter.channels()
    assert caught.value.category == 'permission_denied'
    assert 'never-expose' not in str(caught.value)
    assert not hub.server.writes


def test_create_posts_the_single_key_hub_payload_once_and_returns_for_async_readback(hub):
    assert create_fixture(hub.adapter) is None
    request, = hub.server.writes
    assert (request.method, request.url.path) == ('POST', '/api/admin-hub/channels')
    body = json.loads(request.content)
    assert body['name'] == 'stable-hub-fixture'
    assert body['key'] == 'fixture-upstream-key'
    assert body['platform_channel_type'] == 'openai_gpt'
    assert body['type'] == 1 and body['model_series'] == 'openai.gpt'
    assert body['models'] == ['gpt-4o'] and body['group'] == 'default'
    assert body['siteIds'] == [4]
    assert not {'mode', 'channel', 'owner_user_id', 'supplier_id'} & body.keys()
    assert hub.server.events[-2:] == ['before_write', ('POST', '/api/admin-hub/channels')]

    hub.server.items = [deepcopy(hub.server.remote)]
    hub.server.publishing['11']['has_active_tasks'] = True
    with pytest.raises(RemoteError):
        remote = hub.adapter.find_unique_name('stable-hub-fixture')
        hub.adapter.validate_created_config(remote, {'status': 2}, '', 1)
    assert len(hub.server.writes) == 1
    hub.server.publishing['11']['has_active_tasks'] = False
    remote = hub.adapter.find_unique_name('stable-hub-fixture')
    hub.adapter.validate_created_config(remote, {'status': 2}, '', 1)
    assert len(hub.server.writes) == 1


@pytest.mark.parametrize('create_status', [1, None, '2', True])
def test_create_requires_a_verified_matching_downstream_default_status(hub, create_status):
    hub.server.publish_fields['site_defaults']['create_status'] = create_status
    with pytest.raises(RemoteError):
        create_fixture(hub.adapter)
    assert not hub.server.writes


@pytest.mark.parametrize('change', [
    {'keys': ['fixture-key-one', 'fixture-key-two']},
    {'proxy': 'http://fixture-proxy.example:8080'},
    {'config': {'status': 2, 'model_mapping': {'gpt-4o': 'another-model'}}},
])
def test_unrepresentable_create_options_are_rejected_without_silent_dropping(hub, change):
    with pytest.raises(RemoteError):
        create_fixture(hub.adapter, **change)
    assert not hub.server.writes


@pytest.mark.parametrize('enabled,expected_status', [(True, 1), (False, 2)])
def test_toggle_targets_the_unique_published_downstream_mapping(hub, enabled, expected_status):
    hub.adapter.toggle('11', enabled)
    request, = hub.server.writes
    assert (request.method, request.url.path) == ('POST', '/api/admin-hub/channels/11/status')
    assert json.loads(request.content) == {'status': expected_status, 'site_id': 4}
    assert hub.server.events[-2:] == ['before_write', ('POST', request.url.path)]


def test_selected_model_test_uses_hub_post_and_sanitizes_the_result(hub):
    result = hub.adapter.test_channel('11', 'gpt-4o', expected=hub.server.expected)
    request, = hub.server.writes
    assert (request.method, request.url.path) == ('POST', '/api/admin-hub/channels/11/test')
    assert json.loads(request.content) == {'site_id': 4, 'model': 'gpt-4o'}
    assert result['success'] is True and result['latency_ms'] == 125
    assert 'never-expose' not in json.dumps(result)
    assert hub.server.events[-2:] == ['before_write', ('POST', request.url.path)]


def test_realtime_usage_reads_the_unique_mapping_without_settlement_or_cached_status_claims(hub):
    result = hub.adapter.usage('11', expected=hub.server.expected)
    assert result['used_quota'] == 1250000
    assert result['settlement_verified'] is False
    assert result['used_amount'] is None
    assert any(request.url.path == '/api/admin-hub/channels/11/realtime-used-quota'
               for request in hub.server.requests)
    assert not hub.server.writes


@pytest.mark.parametrize('case', ['multiple', 'active', 'unknown_publish', 'missing_remote_id', 'mapping_mismatch'])
def test_ambiguous_or_unfinished_publication_blocks_operations(hub, case):
    if case == 'multiple':
        hub.server.remote['selected_site_ids'].append(5)
        hub.server.remote['sites'].append({'site_id': 5, 'remote_channel_id': 22,
                                          'remote_status': 2, 'last_publish_status': 'created'})
    elif case == 'active':
        hub.server.publishing['11']['has_active_tasks'] = True
    elif case == 'unknown_publish':
        hub.server.publishing['11']['sites'][0]['publish_status'] = 'future-state'
    elif case == 'missing_remote_id':
        hub.server.remote['sites'][0]['remote_channel_id'] = 0
    else:
        hub.server.publishing['11']['sites'][0]['remote_channel_id'] = 99
    for operation in (
        lambda: hub.adapter.toggle('11', False),
        lambda: hub.adapter.test_channel('11', 'gpt-4o', expected=hub.server.expected),
        lambda: hub.adapter.usage('11', expected=hub.server.expected),
    ):
        with pytest.raises(RemoteError):
            operation()
    assert not hub.server.writes


@pytest.mark.parametrize('change', [
    {'id': 12}, {'name': 'reused-hub-id'}, {'type': 14}, {'owner_user_id': 99}, {'supplier_id': 99},
])
def test_foreign_or_changed_identity_blocks_test_and_usage(hub, change):
    hub.server.remote.update(change)
    for operation in (
        lambda: hub.adapter.test_channel('11', 'gpt-4o', expected=hub.server.expected),
        lambda: hub.adapter.usage('11', expected=hub.server.expected),
    ):
        with pytest.raises(RemoteError):
            operation()
    assert not hub.server.writes


def test_fresh_build_change_before_the_write_stops_the_operation(hub):
    hub.adapter.verify()
    hub.adapter.frozen_build_id = spacex_hub_build.BUILD_ID
    def change_build(request):
        if request.url.path == '/api/admin-hub/channels/11':
            hub.server.build = 'unreviewed-build'
    hub.server.on_request = change_build
    with pytest.raises(RemoteError):
        hub.adapter.toggle('11', False)
    assert not hub.server.writes


@pytest.mark.parametrize('operation', ['create', 'toggle', 'test'])
def test_before_write_hook_can_cancel_without_a_remote_mutation(hub, operation):
    def stop():
        raise RuntimeError('fixture lease lost')
    hub.adapter.before_write = stop
    with pytest.raises(RuntimeError, match='fixture lease lost'):
        if operation == 'create':
            create_fixture(hub.adapter)
        elif operation == 'toggle':
            hub.adapter.toggle('11', False)
        else:
            hub.adapter.test_channel('11', 'gpt-4o', expected=hub.server.expected)
    assert not hub.server.writes


def test_delete_and_key_rotation_never_fall_back_to_other_remote_routes(hub):
    for operation in (
        lambda: hub.adapter.delete('11', expected=hub.server.expected),
        lambda: hub.adapter.edit('11', changes={}, key='replacement-fixture-key', expected=hub.server.expected),
    ):
        with pytest.raises(RemoteError) as caught:
            operation()
        assert caught.value.category == 'unsupported'
    assert not hub.server.writes


@pytest.mark.parametrize('cached_status', [1, 2, 3])
def test_archive_cannot_treat_hub_cached_status_as_fresh_remote_confirmation(hub, cached_status):
    hub.server.remote['status'] = cached_status
    hub.server.remote['sites'][0]['remote_status'] = cached_status
    detail = hub.adapter.detail('11')
    assert detail['_hub_status_source'] == 'cached_downstream_status'
    assert 'key' not in detail and 'key_preview' not in detail
    assert hub.adapter.channel_status('11', expected=hub.server.expected) is None
    assert not hub.server.writes


def test_public_build_fingerprints_are_read_without_credentials_and_changed_assets_fail(monkeypatch):
    path = '/static/js/fixture-hub.js'
    content = b'fixture-public-hub-contract'
    monkeypatch.setattr(spacex_hub_build, 'SCRIPT_PATHS', (path,))
    monkeypatch.setattr(spacex_hub_build, 'ASSETS', ((path, hashlib.sha256(content).hexdigest(), len(content)),))
    requests = []
    changed = False
    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == httpx.URL(spacex_hub_build.ORIGIN).host
        assert not {'authorization', 'new-api-user', 'cookie'} & set(request.headers.keys())
        if request.url.path == '/':
            return httpx.Response(200, text=f'<html><script src="{path}"></script></html>')
        assert request.url.path == path
        return httpx.Response(200, content=content + b'changed' if changed else content)
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        assert spacex_hub_build.identify_build(
            spacex_hub_build.ORIGIN, {'version': ''}, client.request) == spacex_hub_build.BUILD_ID
        changed = True
        with pytest.raises(RemoteError) as caught:
            spacex_hub_build.identify_build(spacex_hub_build.ORIGIN, {'version': ''}, client.request)
        assert caught.value.reason == 'unrecognized_build'
        count = len(requests)
        with pytest.raises(RemoteError):
            spacex_hub_build.identify_build('https://unreviewed.example', {'version': ''}, client.request)
        assert len(requests) == count


def detached_site(hub):
    result = hub.adapter.verify()
    return Site(id='fixture-detached-site', display_id=1, name='Hub fixture', prefix='HUB',
        base_url=spacex_hub_build.ORIGIN, adapter='spacex-hub-v1', routing_group='default',
        seller_user_id='49', token_encrypted=hub.site.token_encrypted, token_hint='••••',
        enabled=True, archived=False, health='healthy', collect_enabled=True,
        capabilities=result['capabilities'], stats_config={}, verified_at=utcnow(), created_at=utcnow())


def test_verified_build_and_site_projection_use_public_metadata_without_exposing_credentials(hub):
    site = detached_site(hub)
    assert build_snapshot(site) == {'site_build_id': spacex_hub_build.BUILD_ID}
    projected = site_json(site)
    assert projected['verified_build'] == dict(spacex_hub_build.BUILD)
    assert projected['template_options']['groups'] == ['default']
    assert projected['template_options']['models'] == ['gpt-4o']
    assert 'token_encrypted' not in projected and 'capabilities' not in projected
    assert 'fixture-hub-management-token' not in str(projected)
    public = site_json(site, privileged=False)
    assert not {'seller_user_id', 'base_url', 'token_hint', 'verified_build'} & public.keys()
    site.base_url = 'https://unreviewed.example'
    assert build_snapshot(site) == {}
    assert site_json(site)['verified_build'] is None


def test_upload_preparation_rejects_incompatible_hub_status_mapping_and_vertex_service(hub):
    site = detached_site(hub)
    fmt = SimpleNamespace(schema_config={'type': 'api_key', 'remote_type': 1})
    assert target_issues(site, fmt, ['gpt-4o'], channel_config={'status': 2}) == []
    for config in ({'status': 1}, {'status': 2, 'model_mapping': {'gpt-4o': 'other-model'}}):
        assert any('SpaceX' in issue for issue in target_issues(site, fmt, ['gpt-4o'], channel_config=config))
    vertex = SimpleNamespace(schema_config={'type': 'vertex_claude', 'remote_type': 41})
    issues = target_issues(site, vertex, ['claude-sonnet-4-6'],
                           channel_config={'status': 2, 'other': '{"default":"us-east5"}'})
    assert any('SpaceX' in issue and 'Claude' in issue for issue in issues)


@pytest.mark.parametrize('operation', ['toggle', 'test'])
def test_revoked_user_channel_write_blocks_existing_channel_operations(hub, operation):
    hub.server.identity['admin_hub']['capabilities']['channel_write'] = False
    with pytest.raises(RemoteError) as caught:
        if operation == 'toggle':
            hub.adapter.toggle('11', False)
        else:
            hub.adapter.test_channel('11', 'gpt-4o', expected=hub.server.expected)
    assert caught.value.category == 'permission_denied'
    assert not hub.server.writes


def test_channels_reads_detail_and_publication_before_projecting_cached_state(hub):
    hub.server.items = [{**hub.server.remote, 'status': 1}]
    hub.server.remote['sites'][0]['remote_status'] = 3
    channel, = hub.adapter.channels()
    assert channel['status'] == 3 and channel['_hub_publication_verified'] is True
    assert channel['_hub_status_source'] == 'cached_downstream_status'
    assert any(request.url.path == '/api/admin-hub/channels/11' for request in hub.server.requests)
    assert not hub.server.writes


def test_raw_vertex_credential_schema_cannot_bypass_the_claude_restriction(hub):
    hub.server.platforms['platform_types'] = [{
        'platform_type_key': 'vertex_gemini', 'platform_display_name': 'Vertex Gemini',
        'new_api_type': 41, 'model_series': 'vertex_ai.gemini', 'key_fields': [],
    }]
    hub.server.overrides[('GET', '/api/admin-hub/channels/model-options')] = httpx.Response(200, json={
        'success': True, 'data': {'platform_type': 'vertex_gemini', 'new_api_type': 41,
            'model_series': 'vertex_ai.gemini', 'items': [{'model_name': 'claude-sonnet-4-6'}]}})
    with pytest.raises(RemoteError, match='Gemini.*Claude'):
        create_fixture(hub.adapter, channel_type=41, models=['claude-sonnet-4-6'],
                       config={'status': 2, 'other': '{"default":"us-east5"}',
                               'credential_format': {'type': 'vertex_json', 'remote_type': 41}})
    assert not hub.server.writes


def provider_create_fixture(hub, provider):
    """Reviewed provider fields and synthetic single credentials, never live keys."""
    cases = {
        'aws_api': (33, 'aws_api_key', 'fixture-api-token|us-east-1', '',
                    'settings.aws_key_type', 'api_key'),
        'aws_iam': (33, 'aws_ak_sk', 'AKIA_TEST|fixture-secret|us-east-1', '',
                    'settings.aws_key_type', 'ak_sk'),
        'azure': (3, 'api_key', 'fixture-azure-key', '2024-12-01-preview', 'other', '2024-12-01-preview'),
        'ai_studio': (24, 'api_key', 'fixture-gemini-key', 'v1beta', 'other', 'v1beta'),
        'vertex': (41, 'vertex_json', json.dumps({'project_id': 'fixture-project',
                    'client_email': 'fixture@example.test', 'private_key': 'fixture-private-key'}),
                    '{"default":"us-central1"}', 'other', '{"default":"us-central1"}'),
    }
    channel_type, kind, key, other, required_field, expected = cases[provider]
    platform_key, series = spacex_hub.PLATFORMS[channel_type]
    platform = {'platform_type_key': platform_key, 'new_api_type': channel_type, 'model_series': series,
                'key_fields': [{'name': required_field, 'type': 'select' if channel_type == 33 else 'text'}]}
    hub.server.platforms['platform_types'] = [platform]
    models = ['gemini-2.5-pro'] if channel_type in (24, 41) else ['gpt-4o'] if channel_type == 3 else ['claude-sonnet-4-6']
    hub.server.overrides[('GET', '/api/admin-hub/channels/model-options')] = httpx.Response(200, json={
        'success': True, 'data': {'platform_type': platform_key, 'new_api_type': channel_type,
            'model_series': series, 'items': [{'model_name': model} for model in models]}})
    config = {'status': 2, 'other': other, 'credential_format': {'type': kind, 'remote_type': channel_type}}
    if channel_type == 3:
        config['base_url'] = 'https://fixture.openai.azure.com'
        platform['key_fields'].append({'name': 'base_url', 'type': 'text', 'required': True})
    return {'channel_type': channel_type, 'key': key, 'models': models, 'config': config}, required_field, expected


@pytest.mark.parametrize('provider', ['aws_api', 'aws_iam', 'azure', 'ai_studio', 'vertex'])
@pytest.mark.parametrize('missing', [None, [], 'note'])
def test_missing_provider_field_blocks_upload_before_any_write(hub, provider, missing):
    args, required_field, _ = provider_create_fixture(hub, provider)
    platform = hub.server.platforms['platform_types'][0]
    platform['key_fields'] = [{'name': required_field, 'type': 'note'}] if missing == 'note' else missing
    with pytest.raises(RemoteError) as caught:
        create_fixture(hub.adapter, **args)
    assert caught.value.reason == 'platform_fields_changed'
    assert not hub.server.writes and 'before_write' not in hub.server.events


@pytest.mark.parametrize('provider', ['aws_api', 'aws_iam', 'azure', 'ai_studio', 'vertex'])
def test_reviewed_provider_fields_preserve_authentication_and_region(hub, provider):
    args, required_field, expected = provider_create_fixture(hub, provider)
    assert create_fixture(hub.adapter, **args) is None
    request, = hub.server.writes
    body = json.loads(request.content)
    assert body[required_field] == expected
    assert body['siteIds'] == [4]


def test_provider_field_removed_during_preflight_does_not_send_create(hub):
    args, _, _ = provider_create_fixture(hub, 'aws_api')
    reads = 0

    def remove_field_on_second_catalog_read(request):
        nonlocal reads
        if request.url.path == '/api/admin-hub/channels/platform-types':
            reads += 1
            if reads == 2:
                hub.server.platforms['platform_types'][0]['key_fields'] = []

    hub.server.on_request = remove_field_on_second_catalog_read
    with pytest.raises(RemoteError) as caught:
        create_fixture(hub.adapter, **args)
    assert caught.value.reason == 'platform_fields_changed'
    assert reads == 2 and not hub.server.writes and 'before_write' not in hub.server.events
