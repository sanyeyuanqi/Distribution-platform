"""Connection checks read endpoints without deciding upload compatibility or permissions."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.security import encrypt

ADAPTERS = (SiliconAdapter, TcpRedAdapter, NewAPIAdapter)


@contextmanager
def remote_case(adapter_type, *, version='v99.2.3-future', overrides=None):
    site = SimpleNamespace(base_url='https://fixture.invalid', seller_user_id='71',
                           token_encrypted=encrypt('fixture-token'), enabled=True,
                           verified_at='old-verification', capabilities={
                               'verified_version': 'old-version', 'create': 'unsupported',
                               'verification_error': {'category': 'protocol_error'}})
    data = {
        '/api/user/self': {'id': 71},  # No role or write-permission fields are needed.
        '/api/status': {'version': version},
        '/api/seller/channel/': {'items': [{}], 'total': 1, 'can_write': 'not a permission'},
        '/api/seller/channel/meta': {'models': [], 'groups': []},
        '/api/channel/': {'items': [{}], 'total': 1},
        '/api/channel/models': [],
        '/api/group/': [],
    }
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'fixture.invalid'
        path = request.url.path
        assert path in data, f'Unexpected endpoint {path}'
        assert request.headers['Authorization'] == 'Bearer fixture-token'
        if path in (overrides or {}):
            response = overrides[path]
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json={'success': True, 'data': data[path]})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        adapter = adapter_type(site, transport=client.request,
                               before_write=lambda: pytest.fail('Connection checks cannot write'))
        yield SimpleNamespace(adapter=adapter, site=site, calls=calls, data=data)


@pytest.mark.parametrize(('adapter_type', 'version'), [
    (adapter_type, version) for adapter_type in ADAPTERS for version in ('v99.2.3-future', '', None)
])
def test_connection_uses_only_configured_reads_without_compatibility_or_site_mutation(monkeypatch, adapter_type, version):
    def forbidden(*args, **kwargs):
        pytest.fail('Connection checks must not perform capability or build verification')
    monkeypatch.setattr('app.adapters.new_api.identify_build', forbidden)
    with remote_case(adapter_type, version=version) as case:
        original = deepcopy(vars(case.site))
        for method in ('verify', 'permissions', 'metadata', 'channels', 'detail', '_seller_version',
                       '_version', '_page', 'supported_types', 'usage_conversion'):
            monkeypatch.setattr(case.adapter, method, forbidden, raising=False)
        result = case.adapter.check_connection()
        expected = ['/api/user/self', '/api/status']
        expected += (['/api/seller/channel/', '/api/seller/channel/meta'] if adapter_type is SiliconAdapter
                     else ['/api/channel/', '/api/channel/models', '/api/group/'])
        assert result == {'version': version or None, 'endpoints': expected}
        assert [request.url.path for request in case.calls] == expected
        assert vars(case.site) == original
        assert not hasattr(case.adapter, '_seller_contract_profile')
        assert not hasattr(case.adapter, '_current_version')
        listing = case.calls[2]
        assert dict(listing.url.params) == {'p': '1', 'page_size': '1'}
        if adapter_type is not SiliconAdapter:
            assert dict(case.calls[-1].url.params) == ({'scope': 'channel'} if adapter_type is TcpRedAdapter else {})


@pytest.mark.parametrize('version', ['secret-token', 'v1.2.3?token=secret', 'v1.2.3-' + 'a' * 80, 123, {}])
def test_unsafe_or_non_version_status_is_not_exposed(version):
    with remote_case(SiliconAdapter, version=version) as case:
        assert case.adapter.check_connection()['version'] is None


@pytest.mark.parametrize(('path', 'data'), [
    ('/api/user/self', []), ('/api/status', []),
    ('/api/seller/channel/', []), ('/api/seller/channel/', {'items': {}, 'total': 0}),
    ('/api/seller/channel/', {'items': [], 'total': True}),
    ('/api/seller/channel/', {'items': [], 'total': -1}),
    ('/api/seller/channel/meta', []), ('/api/seller/channel/meta', {'models': {}, 'groups': []}),
    ('/api/seller/channel/meta', {'models': [], 'groups': None}),
])
def test_invalid_basic_read_shapes_report_exact_safe_endpoint(path, data):
    response = httpx.Response(200, json={'success': True, 'data': data})
    with remote_case(SiliconAdapter, overrides={path: response}) as case:
        with pytest.raises(RemoteError) as caught:
            case.adapter.check_connection()
        assert caught.value.category == 'protocol_error'
        assert caught.value.endpoint == path and caught.value.method == 'GET'
        assert case.calls[-1].url.path == path


@pytest.mark.parametrize(('adapter_type', 'path'), [
    (TcpRedAdapter, '/api/channel/models'), (NewAPIAdapter, '/api/group/'),
])
def test_management_metadata_requires_array_container(adapter_type, path):
    response = httpx.Response(200, json={'success': True, 'data': {}})
    with remote_case(adapter_type, overrides={path: response}) as case:
        with pytest.raises(RemoteError) as caught:
            case.adapter.check_connection()
        assert caught.value.category == 'protocol_error' and caught.value.endpoint == path


@pytest.mark.parametrize('identity', [{'id': 72}, {'id': True}, {'id': '71'}, {}, {'id': -1}])
def test_identity_must_match_configured_account(identity):
    response = httpx.Response(200, json={'success': True, 'data': identity})
    with remote_case(SiliconAdapter, overrides={'/api/user/self': response}) as case:
        with pytest.raises(RemoteError) as caught:
            case.adapter.check_connection()
        assert caught.value.category == 'identity_mismatch'
        assert caught.value.endpoint == '/api/user/self' and len(case.calls) == 1


@pytest.mark.parametrize(('response', 'category', 'status'), [
    (httpx.Response(401, text='secret-token'), 'authentication_error', 401),
    (httpx.Response(403, text='secret-token'), 'permission_denied', 403),
    (httpx.Response(500, text='secret-token'), 'server_error', 500),
    (httpx.Response(307, headers={'Location': 'https://other.invalid/secret'}, text='secret-token'), 'protocol_error', 307),
    (httpx.Response(200, text='<html>secret-token</html>'), 'protocol_error', None),
    (httpx.Response(200, json={'success': False, 'message': 'secret-token'}), 'business_error', None),
    (httpx.Response(200, json={'success': 'true', 'data': {}}), 'protocol_error', None),
])
def test_http_and_business_failures_are_not_connection_success(response, category, status):
    path = '/api/status'
    with remote_case(NewAPIAdapter, overrides={path: response}) as case:
        with pytest.raises(RemoteError) as caught:
            case.adapter.check_connection()
        error = caught.value
        assert error.category == category and error.status_code == status
        assert error.endpoint == path and error.method == 'GET'
        assert 'secret' not in str(error) and not error.unknown
        assert len(case.calls) == 2


def test_read_timeout_reports_endpoint_without_marking_unknown_write():
    path = '/api/group/'
    with remote_case(TcpRedAdapter, overrides={path: httpx.ReadTimeout('secret-token')}) as case:
        with pytest.raises(RemoteError) as caught:
            case.adapter.check_connection()
        assert caught.value.category == 'connection_error' and caught.value.endpoint == path
        assert caught.value.retryable and not caught.value.unknown
        assert 'secret' not in str(caught.value)
