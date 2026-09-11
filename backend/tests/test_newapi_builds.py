"""Hidden-version support requires exact public artifacts, never a guessed release."""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters import newapi_builds as builds
from app.adapters import silicon
from app.adapters.bedrock_capabilities import bedrock_api_key_sdk_mode
from app.adapters.channel_observation import (
    extract_usage,
    normalize_usage_observation,
    site_usage_conversion,
)
from app.adapters.new_api import NewAPIAdapter
from app.adapters.newapi_compatibility import RELEASES
from app.adapters.silicon import RemoteError
from app.adapters.vertex_capabilities import vertex_claude_api_key_capability
from app.channel_service import channel_snapshot
from app.credential_containers import partition_entries
from app.db import utcnow
from app.force_deletion import remote_delete_target
from app.models import Site
from app.models_channels import Task, TaskItem
from app.routers import sites
from app.security import encrypt
from test_newapi_adapter import RemoteFixture, config_for, key_for


@pytest.fixture
def hidden(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live HTTP forbidden in build-profile tests')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)
    # Tiny substitutes exercise all digest/size checks without bundling nearly
    # 1 MiB of third-party production JavaScript into the unit test suite.
    artifacts = {path: ('reviewed artifact ' + path).encode() for path, _, _ in builds.ASSETS}
    monkeypatch.setattr(builds, 'ASSETS', tuple((path, hashlib.sha256(data).hexdigest(), len(data))
                                              for path, data in artifacts.items()))
    server = RemoteFixture('')
    server.identity['permissions'] = {'admin_permissions': {'channel': None}}
    state = SimpleNamespace(server=server, artifacts=artifacts, public=[], before=[],
        html=''.join(f'<script defer src="{path}"></script>' for path in builds.SCRIPT_PATHS),
        groups=['普通', '高倍率'],
        public_response=None, status_response=None, detail_response=None, mutate=lambda request: None)
    def handle(request):
        state.mutate(request)
        path = request.url.path
        if path == '/' or path.startswith('/static/'):
            state.public.append(request)
            assert 'Authorization' not in request.headers and 'New-Api-User' not in request.headers
            assert request.method == 'GET'
            if state.public_response is not None:
                return state.public_response
            return httpx.Response(200, content=state.html.encode() if path == '/' else state.artifacts[path])
        if path == '/api/status':
            server.requests.append(request)
            return state.status_response or httpx.Response(200, json={'success': True,
                'data': {'version': server.version, 'quota_per_unit': 500000, 'usd_exchange_rate': 7.3}})
        if path == '/api/group/':
            server.requests.append(request)
            return httpx.Response(200, json={'success': True, 'data': state.groups})
        if path == '/api/channel/91' and state.detail_response is not None:
            server.requests.append(request)
            return state.detail_response
        if path == '/api/channel' and request.method == 'POST':
            server.requests.append(request)
            return httpx.Response(307, headers={'Location': '/api/channel/'})
        if path == '/api/channel/' and request.method == 'POST':
            server.requests.append(request)
            if server.write_response is not None:
                return server.write_response
            body = json.loads(request.content)
            server.remote = {'id': 91, **body['channel']}
            server.rows = [server.remote]
            return httpx.Response(200, json={'success': True, 'data': {'id': 91}})
        return server.handle(request)
    site = SimpleNamespace(adapter='new-api-v1', base_url=builds.ORIGIN, seller_user_id='71',
        token_encrypted=encrypt('mock-management-only-token'), capabilities={}, verified_at=utcnow())
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        state.site = site
        state.adapter = NewAPIAdapter(site, transport=client.request, before_write=lambda: state.before.append(True))
        yield state


def create(hidden, **kwargs):
    return hidden.adapter.create(name='stable-name', key=kwargs.pop('key', 'fixture-key'), models=['catalog-model'],
                                 group=kwargs.pop('group', '普通,高倍率'), **kwargs)


def test_hidden_build_verifies_as_distinct_contract_with_real_groups(hidden):
    result = hidden.adapter.verify()
    cap = result['capabilities']
    assert result['verified_version'] == builds.BUILD_ID and builds.BUILD_ID not in RELEASES
    assert cap['verified_build'] == dict(builds.BUILD)
    assert cap['version_contract'] == 'role_admin' and cap['groups'] == ['普通', '高倍率']
    assert cap['channel_types'] == [1, 3, 14, 20, 24, 33, 41]
    assert cap['multi_key'] == 'supported' and cap['vertex_claude_api_key'] == 'unknown'
    assert not bedrock_api_key_sdk_mode('new-api-v1', builds.BUILD_ID)
    assert vertex_claude_api_key_capability('new-api-v1', builds.BUILD_ID) == 'unknown'
    assert [request.url.path for request in hidden.public] == ['/', *hidden.artifacts]
    assert all(r.headers['Authorization'] == 'Bearer mock-management-only-token' for r in hidden.server.requests)
    assert not hidden.before and not hidden.server.writes


@pytest.mark.parametrize('url', ['https://other.example', 'http://sssvip.shop', 'https://sssvip.shop:444',
    'https://sssvip.shop/admin', 'https://sssvip.shop.evil.example', 'https://sssvip.shop?x=1'])
def test_build_fallback_is_exact_origin_only(hidden, url):
    hidden.site.base_url = url
    with pytest.raises(RemoteError):
        builds.identify_build(url, {'version': ''}, hidden.adapter.transport)
    assert not hidden.public and not hidden.server.writes and not hidden.before


@pytest.mark.parametrize('version', ['v1.0.0-rc.99', builds.BUILD_ID, ' ', 'v1.0.0-rc.35-fork', None, []])
def test_only_exact_empty_string_can_request_a_build_fingerprint(hidden, version):
    hidden.server.version = version
    with pytest.raises(RemoteError):
        create(hidden)
    assert not hidden.public and not hidden.server.writes


@pytest.mark.parametrize('payload', [{'success': False, 'data': {'version': ''}},
    {'success': 'true', 'data': {'version': ''}}, {'success': True, 'data': []},
    {'success': True, 'data': {'version': None}}])
def test_invalid_status_envelope_never_becomes_a_known_build(hidden, payload):
    hidden.status_response = httpx.Response(200, json=payload)
    with pytest.raises(RemoteError):
        hidden.adapter.verify()
    assert not hidden.public


@pytest.mark.parametrize('change', ['missing_main', 'new_main', 'extra_main', 'base', 'remote_script', 'duplicate_src'])
def test_current_html_must_reference_only_the_reviewed_entry_chain(hidden, change):
    path = builds.SCRIPT_PATHS[-1]
    if change == 'missing_main': hidden.html = hidden.html.replace(f'<script defer src="{path}"></script>', '')
    elif change == 'new_main': hidden.html = hidden.html.replace(path, '/static/js/index.new.js')
    elif change == 'extra_main': hidden.html += '<script src="/static/js/index.new.js"></script>'
    elif change == 'base': hidden.html += '<base href="https://other.example">'
    elif change == 'remote_script': hidden.html = hidden.html.replace(path, 'https://other.example' + path)
    else: hidden.html = hidden.html.replace(f'src="{path}"', f'src="{path}" src="/new.js"')
    with pytest.raises(RemoteError) as error:
        create(hidden)
    assert error.value.reason == 'unrecognized_build'
    assert len(hidden.public) == 1 and not hidden.server.writes


@pytest.mark.parametrize('asset_index', range(3))
@pytest.mark.parametrize('change', ['same_length', 'oversize', 'missing_byte'])
def test_every_reviewed_asset_requires_exact_bytes_and_size(hidden, asset_index, change):
    path = list(hidden.artifacts)[asset_index]
    data = hidden.artifacts[path]
    hidden.artifacts[path] = b'x' + data[1:] if change == 'same_length' else data + b'x' if change == 'oversize' else data[:-1]
    with pytest.raises(RemoteError):
        create(hidden)
    assert not hidden.before and not hidden.server.writes


@pytest.mark.parametrize('status', [301, 302, 307, 404, 500])
def test_public_build_reads_never_follow_redirects_or_accept_error_pages(hidden, status):
    hidden.public_response = httpx.Response(status, headers={'Location': 'https://other.example'})
    with pytest.raises(RemoteError):
        hidden.adapter.verify()
    assert len(hidden.public) == 1 and str(hidden.public[0].url) == builds.ORIGIN + '/'


@pytest.mark.parametrize('identity', [{'id': 71, 'role': 1, 'status': 1}, {'id': 71, 'role': True, 'status': 1},
    {'id': 71, 'role': 10, 'status': 2}, {'id': 72, 'role': 100, 'status': 1},
    {'id': '71', 'role': 100, 'status': 1}])
def test_known_assets_do_not_bypass_enabled_admin_identity(hidden, identity):
    hidden.server.identity = identity
    with pytest.raises(RemoteError):
        create(hidden)
    assert not hidden.before and not hidden.server.writes


@pytest.mark.parametrize('role', [10, 100])
def test_admin_role_contract_create_edit_delete_toggle_remains_explicit(hidden, role):
    hidden.server.identity['role'] = role
    assert create(hidden) == '91'
    request = hidden.server.writes[-1]
    body = json.loads(request.content)
    assert request.url.path == '/api/channel/' and body['mode'] == 'single'
    assert body['channel']['group'] == '普通,高倍率'
    assert isinstance(body['channel']['settings'], str) and isinstance(body['channel']['setting'], str)
    hidden.adapter.edit('91', changes={'remark': 'updated'})
    assert hidden.server.writes[-1].url.path == '/api/channel/'
    assert 'key' not in json.loads(hidden.server.writes[-1].content)
    hidden.adapter.toggle('91', True)
    assert hidden.server.writes[-1].method == 'PUT' and hidden.server.writes[-1].url.path == '/api/channel/'
    assert json.loads(hidden.server.writes[-1].content) == {'id': 91, 'status': 1}
    hidden.adapter.delete('91', expected={'id': '91', 'name': 'stable-name', 'type': 1})
    assert hidden.server.writes[-1].method == 'DELETE'
    assert hidden.server.writes[-1].url.path == '/api/channel/91'
    assert len(hidden.before) == 4
    assert sum(request.url.path == '/' for request in hidden.public) == 8  # fresh identity + immediately before each write


@pytest.mark.parametrize('multiple', [False, True])
def test_build_bedrock_create_uses_canonical_endpoint_once(hidden, multiple):
    keys = ['AKIA_TEST_1|fixture-secret-1|us-east-1', 'AKIA_TEST_2|fixture-secret-2|us-east-1']
    assert create(hidden, key=keys[0], keys=keys if multiple else None, channel_type=33,
                  config={'credential_format': {'type': 'aws_ak_sk', 'remote_type': 33}}) == '91'
    request, = hidden.server.writes
    assert request.method == 'POST' and request.url.path == '/api/channel/'
    assert hidden.before == [True]
    body = json.loads(request.content)
    assert body['mode'] == ('multi_to_single' if multiple else 'single')
    assert body['channel']['key'] == ('\n'.join(keys) if multiple else keys[0])
    assert body['channel']['type'] == 33 and body['channel']['status'] == 2
    assert json.loads(body['channel']['settings'])['aws_key_type'] == 'ak_sk'
    if multiple:
        assert body['multi_key_mode'] == 'random'


def test_build_legacy_endpoint_redirect_is_not_followed_or_replayed(hidden):
    hidden.adapter.permissions()
    with pytest.raises(RemoteError) as caught:
        hidden.adapter.request('POST', '/api/channel', body={'mode': 'single', 'channel': {}})
    assert caught.value.status_code == 307 and caught.value.category == 'protocol_error'
    request, = hidden.server.writes
    assert request.url.path == '/api/channel' and hidden.before == [True]


def test_build_canonical_endpoint_does_not_forward_credentials_to_a_redirect(hidden):
    hidden.server.write_response = httpx.Response(307, headers={'Location': 'https://unrelated.example/api/channel/'})
    with pytest.raises(RemoteError) as caught:
        create(hidden)
    assert caught.value.status_code == 307
    request, = hidden.server.writes
    assert str(request.url) == builds.ORIGIN + '/api/channel/'
    assert hidden.before == [True]


@pytest.mark.parametrize('operation', ['create', 'edit', 'delete', 'toggle'])
@pytest.mark.parametrize('change', ['artifact', 'version'])
def test_build_change_after_permissions_blocks_every_write_before_attempt_flag(hidden, operation, change):
    def mutate(request):
        if request.url.path == ('/api/group/' if operation == 'create' else '/api/channel/91'):
            if change == 'version': hidden.server.version = 'v0.13.2'
            else: hidden.artifacts[builds.SCRIPT_PATHS[-1]] = b'changed artifact'
    hidden.mutate = mutate
    with pytest.raises(RemoteError):
        if operation == 'create': create(hidden)
        elif operation == 'edit': hidden.adapter.edit('91', changes={'remark': 'updated'})
        elif operation == 'delete': hidden.adapter.delete('91')
        else: hidden.adapter.toggle('91', False)
    assert not hidden.before and not hidden.server.writes


def test_frozen_build_identity_cannot_switch_to_a_known_official_release(hidden):
    hidden.adapter.frozen_build_id = builds.BUILD_ID
    hidden.server.version = 'v0.13.2'
    with pytest.raises(RemoteError) as error:
        create(hidden)
    assert error.value.reason == 'build_changed' and not hidden.server.writes


@pytest.mark.parametrize('kind', ['api_key', 'vertex_json', 'aws_ak_sk', 'aws_api_key'])
def test_other_reviewed_types_keep_real_multikey_containers_without_unproved_sdk_transport(hidden, kind):
    from app.newapi_formats import get_format_specs
    spec = next(spec for spec in get_format_specs() if spec['schema_config']['type'] == kind)
    key = key_for(spec)
    second = (key.replace('fake-pem', 'second-fake-pem') if kind == 'vertex_json'
              else 'SECOND-' + key)
    keys = [key, second]
    create(hidden, key=key, keys=keys, channel_type=spec['remote_type'], config=config_for(spec))
    body = json.loads(hidden.server.writes[-1].content)
    assert body['mode'] == 'multi_to_single' and body['multi_key_mode'] == 'random'
    if kind == 'vertex_json': assert isinstance(json.loads(body['channel']['key']), list)
    if kind == 'aws_api_key': assert json.loads(body['channel']['settings'])['aws_key_type'] == 'api_key'


@pytest.mark.parametrize('count', [1, 2])
def test_vertex_api_key_is_single_and_never_silently_combined(hidden, count):
    schema = {'type': 'vertex_api_key', 'remote_type': 41}
    args = {'key': 'fixture-key', 'keys': ['fixture-key', 'second-fixture-key'] if count > 1 else None, 'channel_type': 41,
            'config': {'credential_format': schema, 'other': '{"default":"global"}'}}
    if count > 1:
        with pytest.raises(RemoteError, match='单密钥'):
            create(hidden, **args)
        assert not hidden.server.writes
    else:
        create(hidden, **args)
        assert json.loads(hidden.server.writes[-1].content)['mode'] == 'single'


@pytest.mark.parametrize('channel_type', [1, 3, 14, 20, 24, 33, 41])
def test_build_supports_only_the_reviewed_business_channel_dtos(hidden, channel_type):
    from app.newapi_formats import get_format_specs
    spec = next(spec for spec in get_format_specs() if spec['remote_type'] == channel_type)
    create(hidden, key=key_for(spec), channel_type=channel_type, config=config_for(spec))
    payload = json.loads(hidden.server.writes[-1].content)['channel']
    assert payload['type'] == channel_type and isinstance(payload['settings'], str)
    hidden.adapter.validate_created_config(hidden.server.remote, config_for(spec), '', channel_type)


def test_vertex_api_key_partitioning_is_build_specific_and_stable(hidden):
    fmt = SimpleNamespace(schema_config={'type': 'vertex_gemini', 'remote_type': 41})
    entries = [{'key': 'key-a'}, {'key': 'key-b'}]
    hidden.site.capabilities = {'verified_version': builds.BUILD_ID, 'verified_build': dict(builds.BUILD)}
    parts = partition_entries(entries, fmt, site=hidden.site)
    assert len(parts) == 2 and [p['key_count'] for p in parts] == [1, 1]
    for cap in ({}, {'verification_error': {'category': 'connection_error'}}, {'verified_version': 'v0.13.2'}):
        hidden.site.capabilities = cap
        assert partition_entries(entries, fmt, site=hidden.site) == parts
    hidden.site.capabilities = {'verified_version': 'v1.0.0-rc.35'}
    hidden.site.base_url = 'https://official.example'
    assert len(partition_entries(entries, fmt, site=hidden.site)) == 1


def test_vertex_api_key_rotation_cannot_bypass_single_key_contract(hidden):
    hidden.server.remote.update(type=41, settings='{"vertex_key_type":"api_key"}')
    with pytest.raises(RemoteError, match='单密钥'):
        hidden.adapter.edit('91', changes={}, key='first\nsecond', multikey=True)
    assert not hidden.before and not hidden.server.writes


@pytest.mark.parametrize('response', ['confirmed_missing', 'permission_404', 'other_error'])
def test_only_exact_authenticated_not_found_response_proves_absence(hidden, response):
    hidden.detail_response = (httpx.Response(404) if response == 'permission_404' else httpx.Response(200,
        json={'success': False, 'data': None,
              'message': 'record not found' if response == 'confirmed_missing' else 'permission denied'}))
    target = {'id': '91', 'name': 'stable-name', 'type': 1}
    if response == 'confirmed_missing':
        assert remote_delete_target(hidden.adapter, target) is None
    else:
        with pytest.raises(RemoteError) as error:
            remote_delete_target(hidden.adapter, target)
        assert error.value.unknown
    assert not hidden.server.writes and not hidden.before


def test_build_usage_uses_real_ratio_without_fx_or_balance_unit_and_old_conversion_stays(hidden):
    result = hidden.adapter.verify()
    hidden.site.capabilities = {**result['capabilities'], 'verified_version': result['verified_version']}
    value = extract_usage({'used_quota': 750000, 'balance': 7.3}, conversion=site_usage_conversion(hidden.site))
    assert value['used_amount'] == '1.5' and value['used_amount_unit'] == 'USD'
    assert value['balance'] == 7.3 and value['balance_unit'] is None
    original = deepcopy(value)
    hidden.site.capabilities['usage_conversion']['quota_per_unit'] = '1000000'
    assert normalize_usage_observation(value, conversion=site_usage_conversion(hidden.site)) == original
    hidden.site.base_url = 'https://other.example'
    assert site_usage_conversion(hidden.site) is None


def test_api_projects_only_reviewed_build_metadata_and_keeps_internal_caps_private(hidden, db, login, monkeypatch):
    from test_site_distribution import add_enabled_template
    site = Site(name='Reviewed', prefix='BUILD', base_url=builds.ORIGIN, adapter='new-api-v1',
        seller_user_id='71', token_encrypted=hidden.site.token_encrypted, enabled=True)
    db.add(site)
    db.flush()
    add_enabled_template(db, site, models=['catalog-model'], group='普通')
    db.commit()
    monkeypatch.setattr(sites, 'get_adapter', lambda row: hidden.adapter)
    response = login('root').post('/api/sites/' + site.id + '/verify')
    assert response.status_code == 200
    public = response.json()
    assert public['verified_version'] is None and public['verified_build'] == dict(builds.BUILD)
    assert public['template_options']['groups'] == ['普通', '高倍率']
    assert public['template_options']['routing_group_max_length'] == 128
    assert 'capabilities' not in public
    assert set(login('admin').get('/api/sites').json()['items'][0]) == {'id', 'display_id', 'name', 'health', 'enabled'}
    db.refresh(site)
    site.capabilities = {**site.capabilities, 'verified_build': {**dict(builds.BUILD), 'label': 'untrusted'}}
    assert sites.site_json(site)['verified_build'] is None


def test_new_snapshot_freezes_build_and_worker_rejects_changed_or_unverified_identity(hidden, db, users):
    result = hidden.adapter.verify()
    site = Site(name='Frozen', prefix='FROZEN', base_url=builds.ORIGIN, adapter='new-api-v1',
        seller_user_id='71', token_encrypted=hidden.site.token_encrypted, enabled=True, verified_at=utcnow(),
        capabilities={**result['capabilities'], 'verified_version': result['verified_version']})
    db.add(site)
    db.flush()
    fmt = SimpleNamespace(code='api_key-v1', version='1', schema_config={'type': 'api_key', 'remote_type': 1})
    channel = SimpleNamespace(models=['catalog-model'], remark='', declaration='', category_id='category')
    snapshot = channel_snapshot(channel, fmt, site)
    assert snapshot['site_build_id'] == builds.BUILD_ID
    task = Task(actor_id=users['root'].id, owner_id=users['root'].id,
        actor_session_version=users['root'].session_version, kind='sync')
    db.add(task)
    db.flush()
    item = TaskItem(task_id=task.id, site_id=site.id, operation='sync', snapshot=snapshot)
    db.add(item)
    db.commit()
    worker.assert_execution(db, task, item)
    site.capabilities = {'verified_version': 'v0.13.2'}
    db.commit()
    with pytest.raises(worker.WriteStopped, match='构建'):
        worker.assert_execution(db, task, item)
    # Previously queued official tasks had no build field; they keep their old contract.
    item.snapshot = {key: value for key, value in snapshot.items() if key != 'site_build_id'}
    db.commit()
    worker.assert_execution(db, task, item)


@pytest.mark.parametrize('change', ['artifact', 'version'])
def test_site_sync_does_not_swallow_a_build_change_as_missing_currency_metadata(hidden, db, users, change):
    reads = []
    def mutate(request):
        if request.url.path == '/api/status':
            reads.append(True)
            if len(reads) == 2:
                if change == 'version': hidden.server.version = 'v0.13.2'
                else: hidden.artifacts[builds.SCRIPT_PATHS[-1]] = b'changed artifact'
    hidden.mutate = mutate
    hidden.adapter.frozen_build_id = builds.BUILD_ID
    site = SimpleNamespace(health='unverified', last_sync_at=None)
    item = SimpleNamespace(snapshot={'site_build_id': builds.BUILD_ID})
    with pytest.raises(RemoteError):
        worker.sync_site(db, hidden.adapter, None, item, users['root'], site)
    assert site.health == 'unverified' and site.last_sync_at is None
    assert not hidden.server.writes


@pytest.mark.parametrize(('groups', 'allowed'), [
    (['a' * 64, 'b' * 63], True), (['a' * 64, 'b' * 64], False),
    (['模' * 128], True), (['模' * 129], False),
    (['😀' * 63, '型' * 64], True), (['😀' * 64, '型' * 64], False),
])
def test_build_group_character_limit_counts_joined_commas_and_unicode_before_multikey_create(hidden, groups, allowed):
    from app.channel_service import target_issues
    hidden.groups = groups
    verified = hidden.adapter.verify()
    hidden.site.capabilities = {**verified['capabilities'], 'verified_version': verified['verified_version']}
    hidden.site.enabled, hidden.site.archived = True, False
    fmt = SimpleNamespace(code='api_key-v1', version='1', schema_config={'type': 'api_key', 'remote_type': 1})
    group = ','.join(groups)
    assert target_issues(hidden.site, fmt, ['catalog-model'], routing_group=group) == (
        [] if allowed else [builds.GROUP_TOO_LONG_MESSAGE])
    args = {'group': group, 'key': 'AK1|fixture-secret1|us-east-1',
            'keys': ['AK1|fixture-secret1|us-east-1', 'AK2|fixture-secret2|us-east-1'],
            'channel_type': 33, 'config': {'credential_format': {'type': 'aws_ak_sk', 'remote_type': 33}}}
    if allowed:
        assert create(hidden, **args) == '91'
        request, = hidden.server.writes
        payload = json.loads(request.content)
        assert payload['channel']['group'] == group and payload['mode'] == 'multi_to_single'
        assert hidden.before == [True]
    else:
        with pytest.raises(RemoteError) as caught:
            create(hidden, **args)
        assert str(caught.value) == builds.GROUP_TOO_LONG_MESSAGE and caught.value.reason == 'group_too_long'
        assert not hidden.server.writes and not hidden.before


@pytest.mark.parametrize('remote_rejects', [False, True])
def test_build_85_character_groups_pass_local_limit_but_remote_database_limit_is_not_assumed(hidden, remote_rejects):
    from app.channel_service import target_issues
    hidden.groups = [letter * size for letter, size in zip('abcdef', [18, 13, 21, 4, 16, 8], strict=True)]
    group = ','.join(hidden.groups)
    assert len(group) == 85
    verified = hidden.adapter.verify()
    hidden.site.capabilities = {**verified['capabilities'], 'verified_version': verified['verified_version']}
    hidden.site.enabled, hidden.site.archived = True, False
    fmt = SimpleNamespace(code='api_key-v1', version='1', schema_config={'type': 'api_key', 'remote_type': 1})
    assert target_issues(hidden.site, fmt, ['catalog-model'], routing_group=group) == []
    args = {'group': group, 'key': 'AK1|fixture-secret1|us-east-1',
            'keys': ['AK1|fixture-secret1|us-east-1', 'AK2|fixture-secret2|us-east-1'],
            'channel_type': 33, 'config': {'credential_format': {'type': 'aws_ak_sk', 'remote_type': 33}}}
    if remote_rejects:
        hidden.server.write_response = httpx.Response(200, json={'success': False,
            'message': "Error 1406 (22001): Data too long for column 'group' at row 1"})
        with pytest.raises(RemoteError) as caught:
            create(hidden, **args)
        assert str(caught.value) == '目标站点拒绝了过长的渠道分组，请减少所选分组或联系站点提高长度上限'
        assert caught.value.reason == 'group_too_long'
        assert '128' not in str(caught.value) and '64' not in str(caught.value)
        assert not caught.value.unknown and not caught.value.retryable
    else:
        assert create(hidden, **args) == '91'
    request, = hidden.server.writes
    payload = json.loads(request.content)
    assert payload['channel']['group'] == group and payload['mode'] == 'multi_to_single'
    assert hidden.before == [True]


@pytest.mark.parametrize('operation', ['direct_create', 'direct_edit', 'edit_remark'])
def test_build_group_limit_cannot_be_bypassed_by_direct_adapter_requests_or_copied_edit_config(hidden, operation):
    group = 'a' * 64 + ',' + 'b' * 64
    hidden.adapter.permissions()
    with pytest.raises(RemoteError) as caught:
        if operation == 'edit_remark':
            hidden.server.remote['group'] = group
            hidden.adapter.edit('91', changes={'remark': 'safe fixture note'})
        elif operation == 'direct_create':
            hidden.adapter.request('POST', '/api/channel/', body={'mode': 'single', 'channel': {'group': group}})
        else:
            hidden.adapter.request('PUT', '/api/channel/', body={'id': 91, 'group': group})
    assert caught.value.reason == 'group_too_long'
    assert not hidden.server.writes and not hidden.before


@pytest.mark.parametrize(('message', 'mapped'), [
    ("Error 1406 (22001): Data too long for column 'group' at row 1", True),
    ("Error 1406 (22001): Data too long for column 'group' at row 12", True),
    ("Error 1406 (22001): Data too long for column 'key' at row 1", False),
    ("Error 1406 (22001): Data too long for column 'group' at row 1; private-credential-echo", False),
    ("private-credential-echo: Error 1406 (22001): Data too long for column 'group' at row 1", False),
    ("Data too long for column 'group'", False),
])
def test_build_maps_only_exact_group_database_rejection_without_echoing_sql_or_credentials(hidden, message, mapped):
    hidden.server.write_response = httpx.Response(200, json={'success': False, 'message': message})
    with pytest.raises(RemoteError) as caught:
        create(hidden)
    error = caught.value
    assert str(error) == (builds.REMOTE_GROUP_TOO_LONG_MESSAGE if mapped else '远端业务请求未成功')
    assert error.reason == ('group_too_long' if mapped else None)
    assert error.category == ('configuration_error' if mapped else 'business_error')
    assert not error.unknown and not error.retryable  # Explicit business rejection is not a blind replay request.
    assert 'Error 1406' not in str(error) and 'private-credential-echo' not in str(error)
    assert len(hidden.server.writes) == 1 and hidden.before == [True]


def test_build_limit_and_error_mapping_do_not_change_official_release_or_other_adapter_contracts(hidden):
    from app.adapters.tcp_red import TcpRedAdapter
    from app.channel_service import target_issues
    group = 'a' * 129
    hidden.groups = [group]
    hidden.server.version = 'v0.13.2'
    verified = hidden.adapter.verify()
    hidden.site.capabilities = {**verified['capabilities'], 'verified_version': verified['verified_version']}
    hidden.site.enabled, hidden.site.archived = True, False
    fmt = SimpleNamespace(code='api_key-v1', version='1', schema_config={'type': 'api_key', 'remote_type': 1})
    assert target_issues(hidden.site, fmt, ['catalog-model'], routing_group=group) == []
    assert builds.site_group_max_length(hidden.site) == 160
    assert create(hidden, group=group) == '91'
    payload = {'success': False, 'message': "Error 1406 (22001): Data too long for column 'group' at row 1"}
    hidden.server.write_response = httpx.Response(200, json=payload)
    with pytest.raises(RemoteError) as caught:
        create(hidden, group=group)
    assert str(caught.value) == '远端业务请求未成功' and caught.value.reason is None
    for cls in (silicon.SiliconAdapter, TcpRedAdapter):
        adapter = cls(hidden.site)
        assert str(adapter._business_error(payload, method='POST', path='/api/channel/')) == '远端业务请求未成功'


@pytest.mark.parametrize('change', ['origin', 'adapter', 'build', 'unverified'])
def test_build_group_limit_projection_requires_trusted_origin_and_verified_build(hidden, change):
    hidden.site.capabilities = {'verified_version': builds.BUILD_ID, 'verified_build': dict(builds.BUILD),
                                'routing_group_max_length': 3}
    assert builds.site_group_max_length(hidden.site) == 128
    if change == 'origin': hidden.site.base_url = 'https://unrelated.example'
    elif change == 'adapter': hidden.site.adapter = 'tcp-red-v1'
    elif change == 'build': hidden.site.capabilities['verified_build']['id'] = 'unreviewed'
    else: hidden.site.capabilities['verification_error'] = {'category': 'connection_error'}
    assert builds.site_group_max_length(hidden.site) == 160  # Never trust arbitrary cap values as a limit.


def test_build_group_limit_blocks_template_enable_preview_submit_and_site_enable_without_changing_groups(
        hidden, db, login, monkeypatch):
    from app.models import SiteUploadTemplate
    from sqlalchemy import func, select
    from test_site_distribution import add_enabled_template
    hidden.groups = ['a' * 64, 'b' * 64]
    group = ','.join(hidden.groups)
    verified = hidden.adapter.verify()
    site = Site(name='Group limit fixture', prefix='LIMIT', base_url=builds.ORIGIN, adapter='new-api-v1',
        seller_user_id='71', token_encrypted=hidden.site.token_encrypted, enabled=True, verified_at=utcnow(),
        capabilities={**verified['capabilities'], 'verified_version': verified['verified_version']})
    db.add(site)
    db.flush()
    template = add_enabled_template(db, site, models=['catalog-model'], group=group)
    db.commit()
    root, member = login('root'), login('user')
    body = {'site_id': site.id, 'category_id': template.category_id, 'format_id': template.format_id,
            'models': ['catalog-model'], 'routing_group': group, 'enabled': True}
    rejected_create = root.post('/api/upload-templates', json=body)
    assert rejected_create.status_code == 422 and builds.GROUP_TOO_LONG_MESSAGE in rejected_create.json()['detail']
    rejected_enable = root.patch('/api/upload-templates/' + template.id, json={'enabled': True})
    assert rejected_enable.status_code == 422 and builds.GROUP_TOO_LONG_MESSAGE in rejected_enable.json()['detail']
    listed = root.get('/api/upload-templates').json()['items'][0]
    assert listed['routing_group'] == group and listed['issues'] == [builds.GROUP_TOO_LONG_MESSAGE]
    assert listed['ready'] is False
    payload = {'category_id': template.category_id, 'format_id': template.format_id, 'credentials': 'fixture-key'}
    preview = member.post('/api/uploads/simple-preview', json=payload)
    assert preview.status_code == 200, preview.text
    assert not preview.json()['can_submit']
    assert preview.json()['targets'][0]['issues'] == [builds.GROUP_TOO_LONG_MESSAGE]
    rejected_submit = member.post('/api/uploads/simple-submit', json={**payload, 'idempotency_key': 'group-limit-fixture'})
    assert rejected_submit.status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    monkeypatch.setattr(sites, 'get_adapter', lambda row: hidden.adapter)
    enabled = root.patch('/api/sites/' + site.id, json={'enabled': True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['enabled'] is False and enabled.json()['distribution_ready'] is False
    assert any(builds.GROUP_TOO_LONG_MESSAGE in issue for issue in enabled.json()['distribution_issues'])
    assert enabled.json()['template_options']['routing_group_max_length'] == 128
    db.refresh(template)
    assert template.routing_group == group and template.enabled
    assert db.scalar(select(func.count()).select_from(SiteUploadTemplate)) == 1
    assert not hidden.server.writes and not hidden.before


def test_retry_of_frozen_129_character_bedrock_batch_stops_before_lookup_create_or_write_callback(hidden, db, login, monkeypatch):
    from app.models import Category, CredentialFormat, SiteUploadTemplate
    from app.models_channels import Distribution
    from app.newapi_formats import get_format_specs
    from test_worker_http import LocalLockClient
    hidden.groups = [letter * size for letter, size in zip('abcdef', [28, 23, 21, 14, 20, 18], strict=True)]
    group = ','.join(hidden.groups)
    assert len(group) == 129
    verified = hidden.adapter.verify()
    category = Category(name='AWS', family='AWS', active=True)
    db.add(category)
    db.flush()
    spec = next(spec for spec in get_format_specs() if spec['schema_config']['type'] == 'aws_bedrock')
    fmt = CredentialFormat(category_id=category.id, code=spec['code'], name=spec['name'], version='1',
        enabled=True, schema_config=spec['schema_config'], default_models=[])
    site = Site(name='Frozen limit fixture', prefix='FROZENLIMIT', base_url=builds.ORIGIN, adapter='new-api-v1',
        seller_user_id='71', token_encrypted=hidden.site.token_encrypted, enabled=True, verified_at=utcnow(),
        capabilities={**verified['capabilities'], 'verified_version': verified['verified_version']})
    db.add_all([fmt, site])
    db.flush()
    template = SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id, variant='bedrock',
        name='Old 129-character template', enabled=True, models=['catalog-model'], routing_group=group,
        channel_config={'status': 2})
    db.add(template)
    db.commit()
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: None)
    member = login('user')
    # Model an already queued task from before the newly learned site limit.
    # Only the former acceptance rule is substituted; normal ledger, encrypted
    # container, template snapshots, retry authorization and worker gates run.
    with monkeypatch.context() as old_contract:
        old_contract.setattr(builds, 'group_length_issue', lambda *_: None)
        result = member.post('/api/uploads/simple-submit', json={'category_id': category.id, 'format_id': fmt.id,
            'credentials': 'AKIA_ONE|fixture-secret-one|us-east-1\nAKIA_TWO|fixture-secret-two|us-east-1',
            'idempotency_key': 'old-frozen-group-limit'} )
    assert result.status_code == 200, result.text
    item = db.get(TaskItem, result.json()['items'][0]['id'])
    task = db.get(Task, item.task_id)
    dist = db.get(Distribution, item.distribution_id)
    assert item.snapshot['key_count'] == 2 and item.snapshot['routing_group'] == group
    frozen = deepcopy(item.snapshot)
    item.status, item.stage, item.remote_write_attempted = 'failed', 'create_sent', True
    item.error, dist.error, dist.status, task.status = '远端业务请求未成功', '远端业务请求未成功', 'failed', 'failed'
    db.commit()
    retried = member.post('/api/tasks/' + task.id + '/retry')
    assert retried.status_code == 200, retried.text
    calls = []
    class NoRemoteCalls:
        def find_unique_name(self, *args, **kwargs):
            calls.append('lookup')
            pytest.fail('Overlong frozen group must fail before lookup')

        def create(self, *args, **kwargs):
            calls.append('create')
            pytest.fail('Overlong frozen group must fail before credential POST')

    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write: NoRemoteCalls())
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    item, dist = db.get(TaskItem, item.id), db.get(Distribution, dist.id)
    assert item.status == 'failed' and item.stage == 'queued'
    assert item.error == dist.error == builds.GROUP_TOO_LONG_MESSAGE
    assert item.snapshot == frozen and item.remote_write_attempted is True
    assert dist.remote_id is None and not calls and not hidden.before and not hidden.server.writes
    assert db.get(SiteUploadTemplate, template.id).routing_group == group
