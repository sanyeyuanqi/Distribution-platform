"""Colin contract and dispatch regressions using fixture data, never live sites."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters import get_adapter, silicon, supports_adapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.channel_service import channel_snapshot, target_issues
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_channels import Task, TaskItem
from app.routers.sites import SiteCreate, SitePatch
from app.security import encrypt
from pydantic import ValidationError


class ColinFixture:
    def __init__(self):
        self.flags = {'read': True, 'write': True, 'operate': True,
                      'sensitive_write': True, 'sensitive_write_all': False, 'secret_view': False}
        self.identity = {'id': 123, 'status': 1, 'role': 100,
                         'permissions': {'admin_permissions': {'channel': self.flags}}}
        self.version = TcpRedAdapter.VERSION
        self.groups = ['test-supply', 'alternate-supply']
        self.models = [{'id': 'fixture-model'}]
        self.remote = {'id': 77, 'name': 'stable-fixture', 'created_by': 123, 'type': 1,
                       'status': 1, 'key': 'masked-fixture-key', 'models': 'fixture-model',
                       'group': 'test-supply', 'setting': '{"force_format":false}',
                       'settings': '{"force_format":false}', 'priority': 7, 'weight': 3,
                       'remark': 'before'}
        self.pages = {1: {'items': [], 'total': 0}}
        self.created_data = None
        self.requests = []
        self.on_request = lambda request: None

    @property
    def writes(self):
        return [request for request in self.requests if request.method not in ('GET', 'HEAD')]

    def handle(self, request):
        self.requests.append(request)
        self.on_request(request)
        method, path = request.method, request.url.path
        if method == 'GET' and path == '/api/user/self':
            data = self.identity
        elif method == 'GET' and path == '/api/status':
            data = {'version': self.version}
        elif method == 'GET' and path == '/api/channel/models':
            data = self.models
        elif method == 'GET' and path == '/api/group/':
            assert request.url.params['scope'] == 'channel'
            data = self.groups
        elif method == 'GET' and path in ('/api/channel/', '/api/channel/search'):
            data = self.pages.get(int(request.url.params['p']), {'items': [], 'total': 0})
        elif method == 'GET' and path == '/api/channel/77':
            data = self.remote
        elif method == 'POST' and path == '/api/channel':
            return httpx.Response(307, headers={'Location': '/api/channel/'})
        elif method == 'POST' and path == '/api/channel/':
            data = self.created_data
        elif (method, path) in (('PUT', '/api/channel/'), ('POST', '/api/channel/77/status'),
                                ('DELETE', '/api/channel/77')):
            data = None
        else:
            pytest.fail(f'Unexpected fixture endpoint: {method} {path}')
        return httpx.Response(200, json={'success': True, 'data': data})


@pytest.fixture(autouse=True)
def prohibit_remote_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('This test must use a MockTransport, never a remote request')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)


@pytest.fixture
def colin():
    server = ColinFixture()
    site = SimpleNamespace(adapter='tcp-red-v1', base_url='https://colin-fixture.example',
                           seller_user_id='123', token_encrypted=encrypt('fixture-supplier-token'),
                           routing_group='test-supply')
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        yield SimpleNamespace(server=server, site=site, client=client,
                              adapter=TcpRedAdapter(site, transport=client.request))


def create_fixture(adapter):
    return adapter.create(name='stable-fixture', key='fixture-upstream-key',
                          models=['fixture-model'], group='test-supply', remark='fixture remark')


def test_verify_uses_explicit_permissions_exact_colin_version_and_channel_groups(colin):
    outcome = colin.adapter.verify()
    assert outcome['identity'] == {'id': '123'}
    assert outcome['verified_version'] == 'v1.0.0-rc.32-colin'
    cap = outcome['capabilities']
    assert cap['can_write'] and cap['can_toggle'] and cap['can_edit_routing']
    assert cap['create'] == cap['edit'] == cap['delete'] == cap['toggle'] == 'supported'
    assert cap['groups'] == ['test-supply', 'alternate-supply']
    assert cap['models'] == ['fixture-model'] and 'api_key-v1' in cap['formats']
    assert {1, 3, 33, 41, 57, 59, 60}.issubset(cap['channel_types'])
    assert cap['proxy'] == cap['rpm'] == cap['channel_config'] == 'supported'
    assert cap['rpm_runtime'] == 'not_measured' and cap['account_info'] == 'local_only'
    assert cap['test'] == cap['usage'] == 'supported'
    assert all(cap[name] == 'unsupported' for name in ('stats', 'tpm'))
    group_requests = [r for r in colin.server.requests if r.url.path == '/api/group/']
    assert len(group_requests) == 1 and group_requests[0].url.params['scope'] == 'channel'
    assert all(r.headers['New-Api-User'] == '123' for r in colin.server.requests)
    assert all(r.headers['Authorization'] == 'Bearer fixture-supplier-token' for r in colin.server.requests)
    assert not colin.server.writes


@pytest.mark.parametrize('permissions', [None, {}, {'admin_permissions': {}},
                                        {'admin_permissions': {'channel': []}}])
def test_missing_channel_permissions_never_inferred_from_root_role(colin, permissions):
    colin.server.identity['permissions'] = permissions
    with pytest.raises(RemoteError) as caught:
        colin.adapter.verify()
    assert caught.value.category == 'permission_denied' and not colin.server.writes


@pytest.mark.parametrize('value', [False, None, 1, 'true'])
def test_read_permission_requires_literal_true(colin, value):
    colin.server.flags['read'] = value
    with pytest.raises(RemoteError) as caught:
        colin.adapter.verify()
    assert caught.value.category == 'permission_denied'


@pytest.mark.parametrize('flag', ['write', 'sensitive_write'])
@pytest.mark.parametrize('value', [False, None, 1, 'true'])
def test_write_permissions_require_both_literal_true_flags(colin, flag, value):
    colin.server.flags[flag] = value
    cap = colin.adapter.verify()['capabilities']
    assert cap['can_write'] is False and cap['create'] == 'permission_denied'
    with pytest.raises(RemoteError) as caught:
        create_fixture(colin.adapter)
    assert caught.value.category == 'permission_denied' and not colin.server.writes


def test_identity_mismatch_and_unverified_version_fail_closed(colin):
    colin.server.identity['id'] = 456
    with pytest.raises(RemoteError) as caught:
        colin.adapter.verify()
    assert caught.value.category == 'identity_mismatch'
    colin.server.identity['id'] = 123
    colin.server.version = 'unverified-contract-version'
    with pytest.raises(RemoteError) as caught:
        colin.adapter.verify()
    assert caught.value.category == 'protocol_error' and not colin.server.writes


@pytest.mark.parametrize('created_data,expected_id', [(None, None), ({}, None), ({'id': 991}, '991')])
def test_create_uses_colin_wrapper_string_settings_and_disabled_state(colin, created_data, expected_id):
    colin.server.created_data = created_data
    assert create_fixture(colin.adapter) == expected_id
    request, = colin.server.writes
    assert request.method == 'POST' and request.url.path == '/api/channel/'
    body = json.loads(request.content)
    assert body['mode'] == 'single' and set(body) == {'mode', 'channel'}
    channel = body['channel']
    assert channel['key'] == 'fixture-upstream-key' and channel['status'] == 2
    assert channel['group'] == 'test-supply' and channel['type'] == 1
    assert channel['settings'] == '{}' and channel['setting'] == '{}'
    assert not {'created_by', 'priority', 'weight', 'id'} & channel.keys()


@pytest.mark.parametrize('multiple', [False, True])
def test_bedrock_create_uses_canonical_route_once_without_307_replay(colin, multiple):
    keys = ['AKIA_TEST_1|fixture-secret-1|us-east-1', 'AKIA_TEST_2|fixture-secret-2|us-east-1']
    guard_calls = []
    colin.adapter.before_write = lambda: guard_calls.append('before_write')
    colin.server.created_data = {'id': 991}
    assert colin.adapter.create(
        name='stable-bedrock', key=keys[0], keys=keys if multiple else None,
        models=['fixture-model'], group='test-supply', channel_type=33,
        config={'credential_format': {'type': 'aws_ak_sk', 'remote_type': 33}},
    ) == '991'
    request, = colin.server.writes
    assert request.url.path == '/api/channel/' and request.method == 'POST'
    assert guard_calls == ['before_write']
    body = json.loads(request.content)
    assert body['mode'] == ('multi_to_single' if multiple else 'single')
    assert body['channel']['key'] == ('\n'.join(keys) if multiple else keys[0])
    assert body['channel']['type'] == 33
    assert json.loads(body['channel']['settings'])['aws_key_type'] == 'ak_sk'
    if multiple:
        assert body['multi_key_mode'] == 'random'


def test_legacy_create_redirect_is_not_followed_or_replayed(colin):
    guard_calls = []
    colin.adapter.before_write = lambda: guard_calls.append('before_write')
    with pytest.raises(RemoteError) as caught:
        colin.adapter.request('POST', '/api/channel', body={'mode': 'single', 'channel': {}})
    assert caught.value.status_code == 307 and caught.value.category == 'protocol_error'
    request, = colin.server.writes
    assert request.url.path == '/api/channel'
    assert guard_calls == ['before_write']


def test_edit_omits_masked_secret_creator_status_and_preserves_routing_priority(colin):
    colin.adapter.edit('77', changes={'remark': 'after'}, expected={'name': 'stable-fixture'})
    request, = colin.server.writes
    assert request.method == 'PUT' and request.url.path == '/api/channel/'
    body = json.loads(request.content)
    assert body['id'] == 77 and body['remark'] == 'after'
    assert body['settings'] == colin.server.remote['settings']
    assert body['setting'] == colin.server.remote['setting']
    assert not {'key', 'created_by', 'status'} & body.keys()
    assert body['priority'] == 7 and body['weight'] == 3
    assert 'masked-fixture-key' not in request.content.decode()


def test_model_edit_validates_all_existing_groups_and_preserves_original_group_string(colin):
    colin.server.remote['group'] = 'test-supply,alternate-supply'
    colin.adapter.edit('77', changes={'models': 'fixture-model'})
    assert json.loads(colin.server.writes[0].content)['group'] == 'test-supply,alternate-supply'
    colin.server.requests.clear()
    colin.server.remote['group'] = 'test-supply,unknown-supply'
    with pytest.raises(RemoteError):
        colin.adapter.edit('77', changes={'models': 'fixture-model'})
    assert not colin.server.writes


def test_colin_accepts_new_model_names_and_multiple_verified_groups(colin):
    cap = colin.adapter.verify()['capabilities']
    assert cap['custom_models'] is True and cap['model_max_bytes'] == 255
    colin.adapter.create(name='stable-new-model', key='fixture-upstream-key',
                         models=['provider-new-model'], group='test-supply,alternate-supply')
    body = json.loads(colin.server.writes[0].content)['channel']
    assert body['models'] == 'provider-new-model' and body['group'] == 'test-supply,alternate-supply'
    colin.server.requests.clear()
    with pytest.raises(RemoteError, match='分组'):
        colin.adapter.create(name='blocked-group', key='fixture-upstream-key',
                             models=['provider-new-model'], group='test-supply,unavailable')
    assert not colin.server.writes


@pytest.mark.parametrize('models', [[], [''], [' '], [' trailing '], ['x,y'], ['x\ny'], ['x\ry'],
                                   ['x' * 256], ['模' * 86], [None], 'model', ['model'] * 201])
def test_colin_custom_models_still_validate_wire_syntax(colin, models):
    with pytest.raises(RemoteError, match='模型'):
        colin.adapter.create(name='invalid-model', key='fixture-upstream-key',
                             models=models, group='test-supply')
    assert not colin.server.writes


@pytest.mark.parametrize('field', ['setting', 'settings'])
@pytest.mark.parametrize('value', [{}, '[]', 'null', 'invalid json'])
def test_edit_rejects_incompatible_settings_before_writing(colin, field, value):
    colin.server.remote[field] = value
    with pytest.raises(RemoteError) as caught:
        colin.adapter.edit('77', changes={'remark': 'after'})
    assert caught.value.category == 'protocol_error' and not colin.server.writes


@pytest.mark.parametrize('field', ['key', 'created_by', 'status', 'group', 'priority', 'weight'])
def test_edit_changes_cannot_bypass_sensitive_field_allowlist(colin, field):
    with pytest.raises(RemoteError) as caught:
        colin.adapter.edit('77', changes={field: 'unauthorized-change'})
    assert caught.value.category == 'permission_denied' and not colin.server.writes


@pytest.mark.parametrize('operation', ['edit', 'toggle', 'delete'])
@pytest.mark.parametrize('creator', [456, None, '123'])
def test_unowned_or_ambiguous_creator_blocks_every_remote_mutation(colin, operation, creator):
    colin.server.remote['created_by'] = creator
    with pytest.raises(RemoteError) as caught:
        if operation == 'edit':
            colin.adapter.edit('77', changes={'remark': 'after'})
        elif operation == 'toggle':
            colin.adapter.toggle('77', True)
        else:
            colin.adapter.delete('77')
    assert caught.value.category == 'permission_denied' and not colin.server.writes


def test_explicit_sensitive_write_all_permission_allows_foreign_channel(colin):
    colin.server.flags['sensitive_write_all'] = True
    colin.server.remote['created_by'] = 456
    colin.adapter.edit('77', changes={'remark': 'authorized'})
    assert len(colin.server.writes) == 1
    assert 'created_by' not in json.loads(colin.server.writes[0].content)


@pytest.mark.parametrize('changed', [False, True])
def test_delete_requires_the_confirmed_remote_identity_even_with_all_write_permission(colin, changed):
    colin.server.flags['sensitive_write_all'] = True
    expected = {'id': '77', 'name': 'stable-fixture', 'type': 1}
    if changed:
        colin.server.remote['name'] = 'another-channel'
        with pytest.raises(RemoteError) as caught:
            colin.adapter.delete('77', expected=expected)
        assert caught.value.unknown and caught.value.category == 'identity_mismatch'
        assert not colin.server.writes
    else:
        colin.adapter.delete('77', expected=expected)
        assert len(colin.server.writes) == 1 and colin.server.writes[0].method == 'DELETE'


@pytest.mark.parametrize('operation,revoked_flag', [('create', 'sensitive_write'), ('edit', 'write'),
                                                  ('toggle', 'operate'), ('delete', 'sensitive_write')])
def test_current_permissions_are_read_again_after_site_verification(colin, operation, revoked_flag):
    colin.adapter.verify()
    colin.server.flags[revoked_flag] = False
    with pytest.raises(RemoteError) as caught:
        if operation == 'create':
            create_fixture(colin.adapter)
        elif operation == 'edit':
            colin.adapter.edit('77', changes={'remark': 'after'})
        elif operation == 'toggle':
            colin.adapter.toggle('77', False)
        else:
            colin.adapter.delete('77')
    assert caught.value.category == 'permission_denied' and not colin.server.writes


def test_channel_search_paginates_until_total_even_with_short_pages(colin):
    first, second = deepcopy(colin.server.remote), deepcopy(colin.server.remote)
    second.update(id=78, name='other-fixture')
    colin.server.pages = {1: {'items': [first], 'total': 2}, 2: {'items': [second], 'total': 2}}
    assert colin.adapter.find_unique_name('stable-fixture')['id'] == 77
    requests = [r for r in colin.server.requests if r.url.path == '/api/channel/search']
    assert [r.url.params['p'] for r in requests] == ['1', '2']
    assert all(r.url.params['keyword'] == 'stable-fixture' for r in requests)


@pytest.mark.parametrize('mode', ['duplicate_id', 'premature_end', 'invalid_total'])
def test_invalid_pagination_cannot_silently_mark_channels_missing(colin, mode):
    row = deepcopy(colin.server.remote)
    colin.server.pages = {1: {'items': [row], 'total': 2}, 2: {'items': [], 'total': 2}}
    if mode == 'duplicate_id':
        colin.server.pages[2]['items'] = [row]
    elif mode == 'invalid_total':
        colin.server.pages[1]['total'] = '2'
    with pytest.raises(RemoteError) as caught:
        colin.adapter.channels()
    assert caught.value.category == 'protocol_error'


def test_stable_name_conflict_never_selects_arbitrary_channel(colin):
    first, second = deepcopy(colin.server.remote), deepcopy(colin.server.remote)
    second['id'] = 78
    colin.server.pages = {1: {'items': [first, second], 'total': 2}}
    with pytest.raises(RemoteError) as caught:
        colin.adapter.find_unique_name('stable-fixture')
    assert caught.value.unknown and not colin.server.writes


@pytest.mark.parametrize('adapter_code,expected', [('tcp-red-v1', TcpRedAdapter),
                                                  ('silicon-v1', SiliconAdapter), (None, SiliconAdapter)])
def test_adapter_factory_preserves_selection_transport_and_write_guard(colin, adapter_code, expected):
    colin.site.adapter = adapter_code
    guard = lambda: None
    transport = colin.client.request
    adapter = get_adapter(colin.site, transport=transport, before_write=guard)
    assert type(adapter) is expected and supports_adapter(adapter_code)
    assert adapter.transport is transport and adapter.before_write is guard
    colin.site.adapter = 'unknown-provider'
    assert supports_adapter(colin.site.adapter) is False
    with pytest.raises(ValueError):
        get_adapter(colin.site, transport=transport)


@pytest.mark.parametrize('group,valid', [('test-supply', True), ('default', False), ('unlisted', False)])
def test_dispatch_checks_selected_site_group_and_freezes_it_in_snapshot(colin, group, valid):
    site = colin.site
    site.routing_group, site.enabled, site.archived = group, True, False
    site.capabilities = colin.adapter.verify()['capabilities']
    site.verified_at = utcnow()
    fmt = SimpleNamespace(code='api_key-v1', version='1', schema_config={'type': 'api_key', 'remote_type': 1})
    channel = SimpleNamespace(models=['fixture-model'], remark='', declaration='', category_id='fixture-category')
    issues = target_issues(site, fmt, channel.models)
    assert (issues == []) is valid
    if not valid:
        assert any('路由组' in issue and group in issue for issue in issues)
    assert channel_snapshot(channel, fmt, site)['routing_group'] == group


def test_overlong_remarks_fail_in_preview_and_adapter_without_writes(colin):
    site = colin.site
    site.enabled, site.archived, site.verified_at = True, False, utcnow()
    site.capabilities = colin.adapter.verify()['capabilities']
    fmt = SimpleNamespace(code='api_key-v1', schema_config={'type': 'api_key', 'remote_type': 1})
    assert target_issues(site, fmt, ['fixture-model'], remark_length=255) == []
    assert any('255' in issue for issue in target_issues(site, fmt, ['fixture-model'], remark_length=256))
    with pytest.raises(RemoteError) as caught:
        colin.adapter.create(name='stable-fixture', key='fixture-upstream-key', models=['fixture-model'],
                             group='test-supply', remark='x' * 256)
    assert caught.value.category == 'configuration_error' and not colin.server.writes


@pytest.mark.parametrize('group', ['first,second', 'first\nsecond', 'first\rsecond', 'first\n', ' ', 'x' * 161])
def test_site_group_input_rejects_multiple_empty_or_oversized_values(group):
    create = {'name': 'Fixture', 'prefix': 'TCP', 'base_url': 'https://colin-fixture.example',
              'adapter': 'tcp-red-v1', 'seller_user_id': '123', 'token': 'fixture-token', 'routing_group': group}
    with pytest.raises(ValidationError):
        SiteCreate(**create)
    with pytest.raises(ValidationError):
        SitePatch(routing_group=group)


@pytest.fixture
def tcp_catalog(db, users, monkeypatch, colin):
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    category = Category(id=uid(), name='OpenAI', family='OpenAI', active=True)
    db.add(category)
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=category.id, name='TCP fixture API key', code='api_key-v1',
                           version='1', enabled=True, default_models=['fixture-model'],
                           schema_config={'type': 'api_key', 'remote_type': 1})
    site = Site(id=uid(), name='Colin fixture', prefix='TCP', base_url=colin.site.base_url,
                adapter='tcp-red-v1', routing_group='test-supply', seller_user_id='123',
                token_encrypted=colin.site.token_encrypted, enabled=True, health='healthy', verified_at=utcnow(),
                capabilities=colin.adapter.verify()['capabilities'])
    db.add_all([fmt, site])
    db.commit()
    return category, fmt, site


@pytest.mark.parametrize('change_at', ['before_execution', 'before_http_write'])
def test_worker_stops_old_create_when_selected_group_changes(db, login, tcp_catalog, colin, monkeypatch, change_at):
    category, fmt, site = tcp_catalog
    response = login('user').post('/api/uploads/submit', json={
        'category_id': category.id, 'format_id': fmt.id, 'credentials': 'fixture-upstream-key',
        'models': ['fixture-model'], 'idempotency_key': 'tcp-routing-group-snapshot'})
    assert response.status_code == 200, response.text
    item = db.get(TaskItem, response.json()['items'][0]['id'])
    assert item.snapshot['routing_group'] == 'test-supply'
    monkeypatch.setattr(worker, 'get_adapter', lambda row, **kwargs:
                        get_adapter(row, transport=colin.client.request, **kwargs))

    def change_group():
        site.routing_group = 'alternate-supply'
        db.commit()

    if change_at == 'before_execution':
        change_group()
    else:
        def change_during_metadata(request):
            if request.url.path == '/api/channel/models':
                change_group()
        colin.server.on_request = change_during_metadata

    with pytest.raises(worker.WriteStopped, match='路由组'):
        worker.execute_item(db, item)
    assert not colin.server.writes
    # Existing uncertain outcomes remain eligible for read-only reconciliation.
    task = db.get(Task, item.task_id)
    actor, current = worker.assert_execution(db, task, item, write=False)
    assert actor.id == task.actor_id and current.routing_group == 'alternate-supply'
