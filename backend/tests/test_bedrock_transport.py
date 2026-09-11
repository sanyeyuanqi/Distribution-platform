"""Bedrock transport compatibility never changes or mixes credential members."""
import json

import pytest
from app.adapters.channel_config import create_payload, validate_readback
from app.adapters.silicon import RemoteError

CONFIG = {'credential_format': {'type': 'aws_api_key', 'remote_type': 33}}


def payload(**overrides):
    args = {'name': 'bedrock', 'key': 'bearer-one|us-east-1', 'models': ['claude-opus-4-6'],
            'remark': '', 'group': 'default', 'channel_type': 33, 'config': CONFIG,
            'supported_types': {33}, 'bedrock_api_key_sdk': True}
    return create_payload(**{**args, **overrides})


def test_sdk_bearer_container_retains_each_token_and_region():
    keys = ['bearer-one|us-east-1', 'bearer-two|eu-west-1']
    actual = payload(keys=keys)
    assert actual['key'].splitlines() == keys
    assert json.loads(actual['settings']) == {'aws_key_type': 'ak_sk'}
    validate_readback(actual, CONFIG, '', 33, supported_types={33}, bedrock_api_key_sdk=True)
    actual['settings'] = '{"aws_key_type":"api_key"}'
    with pytest.raises(RemoteError) as exc:
        validate_readback(actual, CONFIG, '', 33, supported_types={33}, bedrock_api_key_sdk=True)
    assert exc.value.unknown


def test_unreviewed_transport_keeps_explicit_api_key_contract():
    actual = payload(bedrock_api_key_sdk=False)
    assert actual['key'] == 'bearer-one|us-east-1'
    assert json.loads(actual['settings']) == {'aws_key_type': 'api_key'}


def test_compatibility_mode_does_not_accept_iam_member_in_bearer_partition():
    with pytest.raises(RemoteError):
        payload(keys=['bearer-one|us-east-1', 'AK|secret|us-east-1'])


def test_compatibility_flag_cannot_be_set_from_user_channel_config():
    with pytest.raises(RemoteError):
        payload(config={**CONFIG, 'bedrock_api_key_sdk': True})


def test_sdk_mode_keeps_merged_bedrock_formats_in_separate_remote_containers():
    with pytest.raises(RemoteError, match='认证'):
        payload(config={'credential_format': {'type': 'aws_bedrock', 'remote_type': 33}},
                keys=['bearer-one|us-east-1', 'AK|secret|us-east-1'])


def test_service_format_serializes_vertex_accounts_as_native_array():
    accounts = [{'project_id': 'fixture-project', 'client_email': f'{i}@fixture.example',
                 'private_key': f'fixture-private-{i}'} for i in range(2)]
    actual = payload(channel_type=41, models=['gemini-2.5-flash'], supported_types={41},
                     config={'credential_format': {'type': 'vertex_gemini', 'remote_type': 41},
                             'other': '{"default":"global"}'},
                     keys=[json.dumps(account) for account in accounts])
    assert json.loads(actual['key']) == accounts
    assert json.loads(actual['settings']) == {'vertex_key_type': 'json'}
