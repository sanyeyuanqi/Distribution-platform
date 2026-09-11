"""Offline Azure rotation guards for the three remote management contracts."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters import silicon
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.azure_credentials import azure_credential_fields
from app.security import encrypt


class RotationFixture:
    def __init__(self, adapter_class, version, kind):
        self.version = version
        self.kind = kind
        self.credential = 'fixture-resource|private-replacement-key' + ('|2024-06-01' if kind == 'azure_gpt' else '')
        self.fields = azure_credential_fields(self.credential, kind)
        self.remote = {
            'id': 91, 'name': 'stable-azure-fixture', 'type': self.fields['credential_format']['remote_type'],
            'created_by': 71, 'models': 'deployment-name', 'group': 'default',
            'key': 'masked-old-key', 'status': 2, 'settings': '{}', 'setting': '{}',
            **self.fields['channel_config'],
        }
        self.expected = {name: self.remote[name] for name in ('name', 'type', 'models', 'group', 'base_url', 'other')}
        self.requests, self.events = [], []
        site = SimpleNamespace(base_url='https://mock-management.example', seller_user_id='71',
                               token_encrypted=encrypt('fixture-management-token'), capabilities={})
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.adapter = adapter_class(site, transport=self.client.request,
                                     before_write=lambda: self.events.append('before_write'))

    @property
    def writes(self):
        return [request for request in self.requests if request.method not in ('GET', 'HEAD')]

    def handle(self, request):
        self.requests.append(request)
        self.events.append((request.method, request.url.path))
        assert request.url.host == 'mock-management.example'
        method, path = request.method, request.url.path
        if (method, path) == ('GET', '/api/status'):
            data = {'version': self.version}
        elif (method, path) == ('GET', '/api/user/self'):
            data = {'id': 71, 'status': 1, 'role': 100, 'permissions': {'admin_permissions': {'channel': {
                'read': True, 'write': True, 'operate': True, 'sensitive_write': True,
                'sensitive_write_all': False,
            }}}}
        elif (method, path) == ('GET', '/api/seller/channel/'):
            data = {'items': [], 'total': 0, 'can_write': True, 'can_toggle': True, 'can_edit_routing': True}
        elif (method, path) == ('GET', '/api/seller/channel/meta'):
            data = {'models': [{'id': 'deployment-name'}], 'groups': ['default']}
        elif method == 'GET' and path in ('/api/channel/91', '/api/seller/channel/91'):
            data = deepcopy(self.remote)
        elif method == 'PUT' and path in ('/api/channel/', '/api/seller/channel/'):
            data = None
        else:
            pytest.fail(f'Unexpected mock endpoint: {method} {path}')
        return httpx.Response(200, json={'success': True, 'data': data})


@pytest.fixture(autouse=True)
def forbid_live_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Azure rotation tests must only use MockTransport')
    monkeypatch.setattr(silicon, 'safe_request', forbidden)


@pytest.fixture(params=[
    (SiliconAdapter, SiliconAdapter.VERSION),
    (SiliconAdapter, 'v1.0.0-rc.25-fix-38'),
    (TcpRedAdapter, TcpRedAdapter.VERSION),
    (NewAPIAdapter, NewAPIAdapter.STABLE_VERSION),
    (NewAPIAdapter, 'v1.0.0-rc.35'),
])
def contract(request):
    return request.param


@pytest.fixture(params=['azure_gpt', 'azure_claude'])
def remote(contract, request):
    fixture = RotationFixture(*contract, request.param)
    yield fixture
    fixture.client.close()


@pytest.mark.parametrize('field', ['base_url', 'other'])
def test_azure_resource_or_version_drift_blocks_rotation_before_put(remote, field):
    remote.remote[field] = ('https://different-resource.example' if field == 'base_url' else '2025-04-01-preview')
    with pytest.raises(RemoteError, match='远端人工修改') as caught:
        remote.adapter.edit('91', changes={}, key=remote.fields['key'], expected=remote.expected)
    assert not remote.writes
    assert 'before_write' not in remote.events
    assert remote.fields['key'] not in str(caught.value)
    assert remote.credential not in str(caught.value)


def test_matching_azure_resource_rotation_sends_only_new_key_and_preserves_connection(remote):
    remote.adapter.edit('91', changes={}, key=remote.fields['key'], expected=remote.expected)
    assert len(remote.writes) == 1
    request = remote.writes[0]
    body = json.loads(request.content)
    assert body['key'] == 'private-replacement-key'
    assert remote.credential not in request.content.decode()
    assert 'masked-old-key' not in request.content.decode()
    assert body['base_url'] == remote.expected['base_url']
    assert body['other'] == remote.expected['other']
    assert body['type'] == remote.expected['type']
    assert remote.events[-2:] == ['before_write', ('PUT', request.url.path)]
