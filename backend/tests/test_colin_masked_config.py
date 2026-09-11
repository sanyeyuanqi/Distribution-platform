"""A successful Colin create may hide its URL; visibility is never verification."""
# ruff: noqa: F811
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters.channel_config import create_payload
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, public_remote
from app.security import encrypt
from test_newapi_adapter import RemoteFixture
from test_tcp_red import colin, prohibit_remote_requests  # noqa: F401


def created_case(adapter, *, base_url=''):
    adapter.site.capabilities = {'can_edit_routing': True}
    schema = {'type': 'aws_ak_sk', 'remote_type': 33}
    config = {'base_url': base_url, 'status': 2}
    remote = create_payload(name='stable-bedrock', key='AKIA_FIXTURE|fixture-secret|us-east-1',
        models=['fixture-model'], remark='', group='test-supply', channel_type=33,
        config={**config, 'credential_format': schema}, **adapter._config_options())
    remote.update(id=77, created_by=123, base_url='***')
    item = SimpleNamespace(operation='create', stage='created_pending_verification', remote_write_attempted=True,
        proxy_encrypted=None, snapshot={'channel_type': 33, 'models': ['fixture-model'],
            'routing_group': 'test-supply', 'channel_config': config, 'format_schema': schema})
    return remote, item, SimpleNamespace(remote_name='stable-bedrock')


@pytest.mark.parametrize('base_url', ['', 'https://private-upstream.example'])
def test_acknowledged_create_checks_visible_config_and_records_hidden_url(colin, base_url):
    remote, item, dist = created_case(colin.adapter, base_url=base_url)
    worker.validate_created(remote, item, dist, colin.adapter)
    assert remote['base_url'] == '***'
    snapshot = public_remote(remote)
    assert snapshot['_unverified_config_fields'] == ['base_url']
    assert not {'base_url', 'key', 'setting', 'settings'} & snapshot.keys()
    assert 'private-upstream' not in json.dumps(snapshot)
    assert not colin.server.writes


@pytest.mark.parametrize(('stage', 'attempted', 'operation'), [
    ('pending', False, 'create'), ('create_sent', True, 'create'), ('reconcile', True, 'create'),
    ('created_pending_verification', False, 'create'), ('created_pending_verification', 1, 'create'),
    ('created_pending_verification', True, 'edit'),
])
def test_hidden_url_without_durable_create_ack_is_not_treated_as_a_match(colin, stage, attempted, operation):
    remote, item, dist = created_case(colin.adapter)
    item.stage, item.remote_write_attempted, item.operation = stage, attempted, operation
    remote['_unverified_config_fields'] = ['base_url']  # Cannot manufacture a local acknowledgement.
    with pytest.raises(RemoteError) as caught:
        worker.validate_created(remote, item, dist, colin.adapter)
    assert caught.value.unknown and caught.value.category == 'configuration_unverified'
    assert '隐藏' in str(caught.value) and '配置与提交快照不同' not in str(caught.value)
    assert not colin.server.writes


@pytest.mark.parametrize(('field', 'value'), [
    ('name', 'different-channel'), ('status', 1), ('models', 'other-model'), ('group', 'other-group'),
    ('settings', '{"aws_key_type":"api_key"}'), ('priority', 99),
    ('base_url', 'https://actually-different.example'), ('base_url', '****'),
])
def test_create_ack_does_not_hide_real_config_differences(colin, field, value):
    remote, item, dist = created_case(colin.adapter)
    remote[field] = value
    with pytest.raises(RemoteError) as caught:
        worker.validate_created(remote, item, dist, colin.adapter)
    assert caught.value.unknown
    assert '_unverified_config_fields' not in remote


@pytest.mark.parametrize('version', ['v0.13.2', 'v1.0.0-rc.35'])
def test_colin_mask_exception_is_not_inherited_by_official_newapi(version):
    server = RemoteFixture(version)
    site = SimpleNamespace(base_url='https://fixture.invalid', seller_user_id='71',
                           token_encrypted=encrypt('fixture-token'))
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as client:
        adapter = NewAPIAdapter(site, transport=client.request)
        adapter.verify()
        remote, item, dist = created_case(adapter)
        with pytest.raises(RemoteError) as caught:
            worker.validate_created(remote, item, dist, adapter)
        assert caught.value.unknown and caught.value.category == 'configuration_mismatch'
        assert not server.writes


@pytest.mark.parametrize('operation', ['edit', 'rotate'])
@pytest.mark.parametrize('field', ['base_url', 'openai_organization', 'other', 'setting', 'settings'])
def test_hidden_connection_values_never_reach_edit_or_rotate_put(colin, operation, field):
    colin.server.remote[field] = '***'
    calls = []
    colin.adapter.before_write = lambda: calls.append('before_write')
    with pytest.raises(RemoteError) as caught:
        colin.adapter.edit('77', changes={'remark': 'updated'} if operation == 'edit' else {},
                           key='replacement-fixture-key' if operation == 'rotate' else None)
    assert caught.value.category == 'configuration_unverified'
    assert not colin.server.writes and not calls


def test_list_and_detail_regenerate_visibility_marker_without_trusting_remote_flags(colin):
    colin.server.remote.update(base_url='***', _unverified_config_fields=['key', 'arbitrary'])
    colin.server.pages = {1: {'items': [deepcopy(colin.server.remote)], 'total': 1}}
    for remote in (colin.adapter.detail('77'), colin.adapter.channels()[0]):
        assert remote['_unverified_config_fields'] == ['base_url']
        assert public_remote(remote)['_unverified_config_fields'] == ['base_url']
    colin.server.remote['base_url'] = 'https://visible.example'
    assert '_unverified_config_fields' not in colin.adapter.detail('77')
    assert not colin.server.writes


@pytest.mark.parametrize('fields', [['key'], ['base_url', 'key'], 'base_url', {'base_url': 'secret'}, None])
def test_public_snapshot_never_copies_arbitrary_visibility_metadata(fields):
    assert public_remote({'id': 77, 'base_url': '***', 'key': 'fixture-secret',
                          '_unverified_config_fields': fields}) == {'id': 77}
