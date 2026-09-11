"""Pinned real HTTP DTOs, including JSON arrays and explicit whole-key replacement."""
import json
from types import SimpleNamespace

import httpx
import pytest
from app.adapters.channel_config import validate_multikey_readback
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.security import encrypt
from test_newapi_adapter import RemoteFixture

PROFILES = [
    ('new-api-v1', 'v0.13.2', NewAPIAdapter),
    ('new-api-v1', 'v1.0.0-rc.35', NewAPIAdapter),
    ('tcp-red-v1', TcpRedAdapter.VERSION, TcpRedAdapter),
    ('silicon-v1', 'v1.0.0-rc.25-fix-36', SiliconAdapter),
    ('silicon-v1', 'v1.0.0-rc.25-fix-38', SiliconAdapter),
]


@pytest.mark.parametrize(('adapter_kind', 'version', 'adapter_class'), PROFILES)
@pytest.mark.parametrize('json_credentials', [False, True])
def test_native_container_create_and_replace_exact_dtos(adapter_kind, version, adapter_class, json_credentials):
    server = RemoteFixture(version)
    site = SimpleNamespace(adapter=adapter_kind, base_url='https://fixture.invalid', seller_user_id='71',
        token_encrypted=encrypt('mock-only-pat'), capabilities={'can_edit_routing': True})
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        adapter = adapter_class(site, transport=client.request)
        cap = adapter.verify()['capabilities']
        assert cap['multi_key'] == 'supported'
        if json_credentials:
            keys = [json.dumps({'project_id': 'project-' + str(index), 'client_email': 'account@example.com',
                                'private_key': 'mock-private-' + str(index)}) for index in range(2)]
            config = {'credential_format': {'type': 'vertex_json', 'remote_type': 41}, 'other': '{"default":"global"}'}
            channel_type = 41
        else:
            keys = ['fixture-key-one', 'fixture-key-two']
            config, channel_type = {'credential_format': {'type': 'api_key', 'remote_type': 1}}, 1
        calls = []
        adapter.before_write = lambda: calls.append('before_write')
        adapter.create(name='stable-name', key=keys[0], keys=keys, models=['catalog-model'], config=config, channel_type=channel_type)
        created = json.loads(server.writes[0].content)
        assert created['mode'] == 'multi_to_single' and created['multi_key_mode'] == 'random'
        wire_key = created['channel']['key']
        assert (all(isinstance(row, dict) for row in json.loads(wire_key)) if json_credentials
                else wire_key == 'fixture-key-one\nfixture-key-two')
        server.remote['created_by'] = 71
        server.remote['channel_info'] = {'is_multi_key': True, 'multi_key_size': 2, 'multi_key_mode': 'random'}
        validate_multikey_readback(server.remote, 2)
        adapter.edit('91', changes={}, key=wire_key, multikey=True,
                     expected={'channel_info': dict(server.remote['channel_info'])})
        replaced = json.loads(server.writes[1].content)
        assert replaced['key_mode'] == 'replace' and replaced['key'] == wire_key
        assert 'channel' not in replaced and 'mode' not in replaced
        assert calls == ['before_write', 'before_write']


@pytest.mark.parametrize('change', [{'multi_key_size': 1}, {'multi_key_mode': 'polling'}, {'is_multi_key': False}, {}])
def test_readback_cannot_silently_downgrade_to_single_or_drop_keys(change):
    info = {'is_multi_key': True, 'multi_key_size': 2, 'multi_key_mode': 'random'}
    remote = {'channel_info': {**info, **change}} if change else {}
    with pytest.raises(RemoteError) as error:
        validate_multikey_readback(remote, 2)
    assert error.value.unknown


def test_unknown_seller_version_with_compatible_interfaces_posts_compact_container():
    server = RemoteFixture('v1.0.0-rc.25-fix-39')
    site = SimpleNamespace(adapter='silicon-v1', base_url='https://fixture.invalid', seller_user_id='71',
                           token_encrypted=encrypt('mock-only-pat'))
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        adapter = SiliconAdapter(site, transport=client.request)
        adapter.create(name='stable-name', key='first', keys=['first', 'second'], models=['catalog-model'])
    assert len(server.writes) == 1
    body = json.loads(server.writes[0].content)
    assert body['mode'] == 'multi_to_single' and body['multi_key_mode'] == 'random'
    assert body['channel']['key'] == 'first\nsecond'


@pytest.mark.parametrize('frozen_sdk', [False, True])
def test_official_bedrock_transport_respects_frozen_old_and_new_profile(frozen_sdk):
    server = RemoteFixture('v1.0.0-rc.35')
    site = SimpleNamespace(adapter='new-api-v1', base_url='https://fixture.invalid', seller_user_id='71',
                           token_encrypted=encrypt('mock-only-pat'), capabilities={'can_edit_routing': True})
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        adapter = NewAPIAdapter(site, transport=client.request)
        adapter.frozen_bedrock_api_key_sdk = frozen_sdk
        adapter.create(name='stable-name', key='api-key|us-east-1', models=['catalog-model'], channel_type=33,
            config={'credential_format': {'type': 'aws_api_key', 'remote_type': 33}})
        payload = json.loads(server.writes[0].content)['channel']
        assert json.loads(payload['settings'])['aws_key_type'] == ('ak_sk' if frozen_sdk else 'api_key')
        assert payload['key'] == 'api-key|us-east-1'
