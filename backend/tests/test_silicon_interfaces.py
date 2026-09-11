"""Compatibility follows seller interfaces; no live requests or credentials."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.security import encrypt
from app.site_verification import safe_diagnostic
from test_newapi_adapter import RemoteFixture


class Interfaces:
    def __init__(self, version='v1.0.0-rc.25-fix-41', mutate=None):
        self.server = RemoteFixture(version)
        self.mutate = mutate or (lambda path, data, count: data)
        self.counts, self.attempts = {}, []
        self.site = SimpleNamespace(adapter='silicon-v1', base_url='https://mock.invalid',
            seller_user_id='71', token_encrypted=encrypt('mock-management-token'), capabilities={})
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.adapter = SiliconAdapter(self.site, transport=self.client.request,
            before_write=lambda: self.attempts.append('write'))

    def handle(self, request):
        response = self.server.handle(request)
        if request.method == 'GET':
            path = request.url.path
            self.counts[path] = self.counts.get(path, 0) + 1
            body = response.json()
            body['data'] = self.mutate(path, deepcopy(body.get('data')), self.counts[path])
            return httpx.Response(response.status_code, json=body)
        return response

    def create(self):
        return self.adapter.create(name='stable-name', key='mock-key', models=['custom-model'])


@pytest.mark.parametrize(('path', 'field', 'value', 'label'), [
    ('/api/status', None, [], 'data'),
    ('/api/seller/channel/', 'items', {}, 'items'),
    ('/api/seller/channel/', 'items', [{'id': True}], 'items.id'),
    ('/api/seller/channel/', 'total', '1', 'total'),
    ('/api/seller/channel/', 'total', -1, 'total'),
    ('/api/seller/channel/', 'can_write', 'true', 'can_write'),
    ('/api/seller/channel/', 'can_toggle', 1, 'can_toggle'),
    ('/api/seller/channel/', 'can_edit_routing', None, 'can_edit_routing'),
    ('/api/seller/channel/meta', 'models', {}, 'models.id'),
    ('/api/seller/channel/meta', 'models', [{'id': 42}], 'models.id'),
    ('/api/seller/channel/meta', 'groups', 'default', 'groups'),
    ('/api/seller/channel/meta', 'groups', [None], 'groups'),
])
def test_incompatible_key_interface_stops_before_write_and_reports_stage(path, field, value, label):
    def mutate(endpoint, data, count):
        if endpoint == path:
            if field is None:
                return value
            data[field] = value
        return data
    fixture = Interfaces(mutate=mutate)
    with fixture.client, pytest.raises(RemoteError) as caught:
        fixture.create()
    error = caught.value
    assert error.category == 'protocol_error' and error.reason == 'incompatible_interface'
    assert error.endpoint == path and error.method == 'GET'
    assert label in str(error)
    diagnostic = safe_diagnostic(error.category, reason=error.reason, endpoint=error.endpoint, method=error.method)
    assert path in diagnostic['message']
    assert not fixture.attempts and not fixture.server.writes


@pytest.mark.parametrize('version', ['v1.0.0-rc.25-fix-41', 'v7.8.9-future', '', None])
def test_compatible_interfaces_preserve_create_edit_toggle_delete_contracts(version):
    fixture = Interfaces(version)
    with fixture.client:
        outcome = fixture.adapter.verify()
        fixture.site.capabilities = {**outcome['capabilities'], 'verified_version': outcome['verified_version']}
        assert outcome['capabilities']['protocol_contract'] == SiliconAdapter.COMPACT_CONTRACT
        fixture.create()
        body = json.loads(fixture.server.writes[-1].content)['channel']
        assert body['models'] == 'custom-model' and isinstance(body['settings'], dict)
        assert 'setting' not in body and 'proxy' in body
        fresh = SiliconAdapter(fixture.site, transport=fixture.client.request)
        fresh.validate_created_config(fixture.server.remote, None, '', 1)
        fresh.edit('91', changes={'remark': 'changed'}, key='replacement')
        edited = json.loads(fixture.server.writes[-1].content)
        assert edited['key'] == 'replacement' and isinstance(edited['settings'], dict)
        assert 'setting' not in edited
        fresh.toggle('91', True)
        assert json.loads(fixture.server.writes[-1].content) == {'status': 1}
        fresh.delete('91', expected={'id': '91', 'name': 'stable-name', 'type': 1})
        assert [r.method for r in fixture.server.writes] == ['POST', 'PUT', 'POST', 'DELETE']
        assert all(r.url.path.startswith('/api/seller/channel/') for r in fixture.server.requests
                   if r.method != 'GET')


@pytest.mark.parametrize('field', SiliconAdapter.PERMISSION_FIELDS)
def test_permission_revocation_at_final_check_never_starts_write(field):
    def mutate(path, data, count):
        if path == '/api/seller/channel/' and count > 1:
            data[field] = False
        return data
    fixture = Interfaces(mutate=mutate)
    with fixture.client, pytest.raises(RemoteError) as caught:
        fixture.create()
    assert caught.value.category == 'permission_denied'
    assert not fixture.attempts and not fixture.server.writes


def test_group_removed_during_final_check_never_starts_write():
    fixture = Interfaces()
    def mutate(path, data, count):
        if path == '/api/seller/channel/meta' and fixture.counts.get('/api/seller/channel/', 0) > 1:
            data['groups'] = ['replacement-group']
        return data
    fixture.mutate = mutate
    with fixture.client, pytest.raises(RemoteError, match='路由组'):
        fixture.create()
    assert not fixture.attempts and not fixture.server.writes


def test_version_only_change_during_request_keeps_compatible_upload_working():
    fixture = Interfaces('v1.0.0-rc.25-fix-38')
    def mutate(path, data, count):
        if path == '/api/seller/channel/meta':
            fixture.server.version = 'v8.9.10-future'
        return data
    fixture.mutate = mutate
    with fixture.client:
        fixture.create()
    assert fixture.adapter._current_seller_version == 'v8.9.10-future'
    assert fixture.attempts == ['write'] and len(fixture.server.writes) == 1


def test_schema_removed_at_final_check_does_not_use_cached_verification():
    def mutate(path, data, count):
        if path == '/api/seller/channel/' and count > 1:
            del data['can_write']
        return data
    fixture = Interfaces(mutate=mutate)
    with fixture.client, pytest.raises(RemoteError, match='can_write'):
        fixture.create()
    assert not fixture.attempts and not fixture.server.writes


@pytest.mark.parametrize('field', ['name', 'type', 'status', 'models', 'group'])
def test_missing_channel_fields_cannot_pass_readonly_reconciliation(field):
    fixture = Interfaces()
    row = deepcopy(fixture.server.remote)
    del row[field]
    fixture.server.rows = [row]
    with fixture.client, pytest.raises(RemoteError, match='items.' + field):
        fixture.adapter.channels()
    assert not fixture.attempts and not fixture.server.writes


def test_readonly_reconciliation_rechecks_identity_even_with_old_cached_capabilities():
    fixture = Interfaces()
    fixture.site.capabilities = {'verified_version': SiliconAdapter.VERSION, 'can_write': True}
    fixture.server.identity['id'] = 99
    with fixture.client, pytest.raises(RemoteError) as caught:
        fixture.adapter.detail('91')
    assert caught.value.category == 'identity_mismatch'
    assert not fixture.attempts and not fixture.server.writes
