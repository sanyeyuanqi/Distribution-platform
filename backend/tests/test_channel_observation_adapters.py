"""Offline model-test and cumulative-counter contracts; no database fixtures."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters import silicon
from app.adapters.channel_observation import extract_usage
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.security import encrypt


class ObservationFixture:
    def __init__(self, adapter_class, version):
        self.version = version
        self.flags = dict.fromkeys(('read', 'write', 'operate', 'sensitive_write'), True)
        self.flags.update(sensitive_write_all=False, secret_view=False)
        self.identity = {'id': 71, 'role': 100, 'status': 1,
                         'permissions': {'admin_permissions': {'channel': self.flags}}}
        self.remote = {'id': 91, 'name': 'stable-name', 'type': 14, 'status': 2,
                       'created_by': 71, 'models': 'claude-sonnet-5,claude-opus-5',
                       'used_quota': 0, 'balance': 1.25, 'balance_updated_time': 1788800000,
                       'key': 'never-expose-me', 'setting': '{"proxy":"password"}'}
        self.events, self.requests = [], []
        self.test_response = httpx.Response(200, json={'success': True, 'time': 0.125})
        self.callback = lambda request: None
        self.site = SimpleNamespace(base_url='https://fixture.example', seller_user_id='71',
                                    token_encrypted=encrypt('fixture-management-token'), capabilities={})
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.adapter = adapter_class(self.site, transport=self.client.request,
                                      before_write=lambda: self.events.append('before_write'))

    @property
    def tests(self):
        return [request for request in self.requests if '/test/' in request.url.path]

    @property
    def expected(self):
        return {'id': '91', 'name': 'stable-name', 'type': 14}

    def handle(self, request):
        self.requests.append(request)
        self.events.append(request.url.path)
        self.callback(request)
        assert request.method == 'GET'
        path = request.url.path
        if '/test/' in path:
            if isinstance(self.test_response, Exception):
                raise self.test_response
            return self.test_response
        if path == '/api/status':
            data = {'version': self.version}
        elif path == '/api/user/self':
            data = self.identity
        elif path == '/api/seller/channel/':
            data = {'items': [], 'total': 0, 'can_write': self.flags['write'],
                    'can_toggle': self.flags['operate'], 'can_edit_routing': self.flags['write']}
        elif path in ('/api/channel/91', '/api/seller/channel/91'):
            data = self.remote
        elif path == '/api/channel/':
            data = {'items': [], 'total': 0}
        elif path == '/api/seller/channel/meta':
            data = {'models': [{'id': 'claude-sonnet-5'}], 'groups': ['default']}
        elif path == '/api/channel/models':
            data = [{'id': 'claude-sonnet-5'}]
        elif path == '/api/group/':
            data = ['default']
        else:
            pytest.fail(f'Unexpected endpoint {path}')
        return httpx.Response(200, json={'success': True, 'data': deepcopy(data)})


@pytest.fixture(autouse=True)
def forbid_live_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live requests forbidden in observation contract tests')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)


@pytest.fixture(params=[(SiliconAdapter, SiliconAdapter.VERSION), (TcpRedAdapter, TcpRedAdapter.VERSION),
                        (SiliconAdapter, 'v1.0.0-rc.25-fix-38'),
                        (NewAPIAdapter, NewAPIAdapter.STABLE_VERSION), (NewAPIAdapter, 'v1.0.0-rc.35')])
def remote(request):
    fixture = ObservationFixture(*request.param)
    yield fixture
    fixture.client.close()


def test_selected_model_is_sent_once_to_own_contract_and_guarded(remote):
    result = remote.adapter.test_channel(91, 'claude-opus-5', expected=remote.expected)
    assert result == {'success': True, 'latency_ms': 125, 'message': '所选模型测试通过'}
    assert len(remote.tests) == 1
    request = remote.tests[0]
    prefix = '/api/seller/channel/test/' if remote.adapter.SELLER_API else '/api/channel/test/'
    assert request.url.path == prefix + '91'
    assert dict(request.url.params) == {'model': 'claude-opus-5'}
    assert request.content == b''
    assert remote.events[-2:] == ['before_write', request.url.path]


@pytest.mark.parametrize('version', sorted(NewAPIAdapter.VERSIONS))
def test_every_reviewed_official_release_supports_the_same_model_query(version):
    remote = ObservationFixture(NewAPIAdapter, version)
    try:
        assert remote.adapter.test_channel(91, 'claude-sonnet-5', expected=remote.expected)['success'] is True
        assert len(remote.tests) == 1
    finally:
        remote.client.close()


def test_old_seller_path_only_documentation_does_not_claim_selected_model_or_usage():
    remote = ObservationFixture(SiliconAdapter, 'v1.0.0-rc.25-fix-22-multiseller-2')
    try:
        cap = remote.adapter.verify()['capabilities']
        assert cap['test'] == 'unsupported'
        assert cap['usage'] == 'unsupported'
        for operation in (lambda: remote.adapter.test_channel(91, 'claude-sonnet-5'),
                          lambda: remote.adapter.usage(91)):
            with pytest.raises(RemoteError) as caught:
                operation()
            assert caught.value.category == 'unsupported'
        assert not remote.tests
    finally:
        remote.client.close()


@pytest.mark.parametrize('change', ['id', 'name', 'type'])
def test_changed_identity_blocks_test_and_usage(remote, change):
    remote.remote[change] = {'id': 92, 'name': 'someone-else', 'type': 1}[change]
    for operation in (lambda: remote.adapter.test_channel(91, 'claude-sonnet-5', expected=remote.expected),
                      lambda: remote.adapter.usage(91, expected=remote.expected)):
        with pytest.raises(RemoteError):
            operation()
    assert not remote.tests
    assert 'before_write' not in remote.events


@pytest.mark.parametrize('model', ['', 'unknown-model', 'claude-sonnet-5\n', 'a,b', None])
def test_invalid_or_unassigned_model_never_invokes_inference(remote, model):
    with pytest.raises(RemoteError):
        remote.adapter.test_channel(91, model)
    assert not remote.tests
    assert 'before_write' not in remote.events


def test_revoked_operate_or_seller_write_permission_blocks_test(remote):
    remote.adapter.verify()
    if isinstance(remote.adapter, NewAPIAdapter) and remote.version in NewAPIAdapter.LEGACY_VERSIONS:
        remote.identity['status'] = 2
        with pytest.raises(RemoteError) as caught:
            remote.adapter.test_channel(91, 'claude-sonnet-5')
        assert caught.value.category == 'permission_denied'
        assert not remote.tests
        return
    flag = 'write' if remote.adapter.SELLER_API else 'operate'
    remote.flags[flag] = False
    with pytest.raises(RemoteError) as caught:
        remote.adapter.test_channel(91, 'claude-sonnet-5')
    assert caught.value.category == 'permission_denied'
    assert remote.adapter.verify()['capabilities']['test'] == 'permission_denied'
    assert not remote.tests


def test_colin_other_suppliers_channel_is_not_tested_or_observed():
    remote = ObservationFixture(TcpRedAdapter, TcpRedAdapter.VERSION)
    try:
        remote.remote['created_by'] = 72
        for operation in (lambda: remote.adapter.test_channel(91, 'claude-sonnet-5'),
                          lambda: remote.adapter.usage(91)):
            with pytest.raises(RemoteError) as caught:
                operation()
            assert caught.value.category == 'permission_denied'
        assert not remote.tests
    finally:
        remote.client.close()


def test_version_changed_after_details_blocks_inference(remote):
    def update_version(request):
        if request.url.path.endswith('/91'):
            remote.version = 'unreviewed-version'
    remote.callback = update_version
    if remote.adapter.SELLER_API:
        remote.adapter.test_channel(91, 'claude-sonnet-5')
        assert len(remote.tests) == 1
        return
    with pytest.raises(RemoteError):
        remote.adapter.test_channel(91, 'claude-sonnet-5')
    assert not remote.tests
    assert 'before_write' not in remote.events


def test_final_guard_can_cancel_inference_without_a_request(remote):
    def stop():
        raise RuntimeError('lease lost')
    remote.adapter.before_write = stop
    with pytest.raises(RuntimeError, match='lease lost'):
        remote.adapter.test_channel(91, 'claude-sonnet-5')
    assert not remote.tests


@pytest.mark.parametrize('response', [
    httpx.Response(500, text='never-expose-me'),
    httpx.Response(200, text='never-expose-me'),
    httpx.Response(200, json={'success': 'true', 'message': 'never-expose-me'}),
    httpx.ReadTimeout('never-expose-me'),
])
def test_uncertain_test_response_is_unknown_and_never_retried(remote, response):
    remote.test_response = response
    with pytest.raises(RemoteError) as caught:
        remote.adapter.test_channel(91, 'claude-sonnet-5')
    assert caught.value.unknown is True
    assert caught.value.retryable is False
    assert 'never-expose-me' not in str(caught.value)
    assert len(remote.tests) == 1
    assert remote.events.count('before_write') == 1


@pytest.mark.parametrize(('payload', 'expected'), [
    ({'success': False, 'time': 0.0, 'message': 'never-expose-me'},
     {'success': False, 'latency_ms': 0, 'message': '所选模型测试未通过'}),
    ({'success': True, 'data': {'response_time': 321, 'key': 'never-expose-me'}, 'time': 5},
     {'success': True, 'latency_ms': 321, 'message': '所选模型测试通过'}),
    ({'success': True, 'time': -1}, {'success': True, 'message': '所选模型测试通过'}),
])
def test_model_test_returns_only_explicit_safe_result(remote, payload, expected):
    remote.test_response = httpx.Response(200, json=payload)
    assert remote.adapter.test_channel(91, 'claude-sonnet-5') == expected


def test_usage_reads_only_original_counters_and_never_refreshes_provider_balance(remote):
    result = remote.adapter.usage(91, expected=remote.expected)
    assert result == extract_usage({'used_quota': 0, 'balance': 1.25, 'balance_updated_time': 1788800000})
    assert not remote.tests
    assert 'before_write' not in remote.events
    assert all('update_balance' not in request.url.path for request in remote.requests)
    assert remote.adapter.verify()['capabilities']['usage'] == 'supported'
    assert remote.adapter.verify()['capabilities']['stats'] == 'unsupported'


@pytest.mark.parametrize('value', [None, {}, {'key': 'secret'},
                                  {'used_quota': '10', 'balance': True, 'balance_updated_time': -1},
                                  {'used_quota': -1, 'balance': float('inf'), 'balance_updated_time': False}])
def test_absent_or_invalid_usage_is_unknown_not_zero(value):
    result = extract_usage(value)
    assert result['used_quota'] is None
    assert result['balance'] is None
    assert result['balance_updated_at'] is None
    assert result['settlement_verified'] is False


def test_usage_preserves_negative_balance_and_original_quota_without_mutation():
    remote = {'used_quota': 1234567, 'balance': -0.5, 'balance_updated_time': 0, 'key': 'secret'}
    before = deepcopy(remote)
    result = extract_usage(remote)
    assert result['used_quota'] == 1234567
    assert result['balance'] == -0.5
    assert result['balance_updated_at'] == 0
    assert result['balance_unit'] is None
    assert remote == before
