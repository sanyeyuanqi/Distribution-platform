import json
from types import SimpleNamespace

import pytest
from app.azure_credentials import azure_credential_fields, normalize_azure_credential
from app.newapi_formats import credential_fingerprint
from app.security import fingerprint


@pytest.mark.parametrize(('kind', 'value', 'expected'), [
    ('azure_gpt', ' My-Resource | opaque-KEY_1+/= | 2024-06-01 ',
     'my-resource|opaque-KEY_1+/=|2024-06-01'),
    ('azure_gpt', 'resource|key|2024-02-29-preview', 'resource|key|2024-02-29-preview'),
    ('azure_claude', ' My-Foundry | opaque-KEY_1+/= ', 'my-foundry|opaque-KEY_1+/='),
    ('azure_claude', 'a1|key', 'a1|key'),
    ('azure_claude', ' My-Foundry | opaque-KEY_1+/= | 2025-04-01-preview ',
     'my-foundry|opaque-KEY_1+/=|2025-04-01-preview'),
    ('azure_claude', 'a' * 64 + '|key', 'a' * 64 + '|key'),
])
def test_normalization_preserves_api_key_and_is_idempotent(kind, value, expected):
    assert normalize_azure_credential(value, kind) == expected
    assert normalize_azure_credential(expected, kind) == expected


@pytest.mark.parametrize(('kind', 'value', 'remote_type', 'url', 'other'), [
    ('azure_gpt', 'My-Resource|private-KEY|2024-06-01', 3,
     'https://my-resource.openai.azure.com', '2024-06-01'),
    ('azure_gpt', 'my-resource|private-KEY|2025-04-01-preview', 3,
     'https://my-resource.openai.azure.com', '2025-04-01-preview'),
    ('azure_claude', 'My-Foundry|private-KEY', 14,
     'https://my-foundry.services.ai.azure.com/anthropic', ''),
    ('azure_claude', 'My-Foundry|private-KEY|2025-04-01-preview', 14,
     'https://my-foundry.services.ai.azure.com/anthropic', ''),
])
def test_remote_fields_contain_only_key_segment_and_derived_public_config(kind, value, remote_type, url, other):
    result = azure_credential_fields(value, kind)
    assert result == {
        'key': 'private-KEY',
        'credential_format': {'type': 'api_key', 'remote_type': remote_type},
        'channel_config': {'base_url': url, 'other': other},
    }
    assert 'private-KEY' not in json.dumps(result['channel_config'])
    assert 'private-KEY' not in json.dumps(result['credential_format'])


@pytest.mark.parametrize('resource', [
    '', 'a', 'a' * 65, '-resource', 'resource-', 'resource.name', 'resource_name',
    'https://resource.openai.azure.com', 'resource/openai', 'resource\\path',
    'resource:443', 'resource@evil.example', 'resource?query=1', 'resource#fragment',
    'resource%2eevil', '资源', 'resource name', 'resource\u200bname',
])
@pytest.mark.parametrize('kind', ['azure_gpt', 'azure_claude'])
def test_resource_cannot_change_official_endpoint(resource, kind):
    value = resource + '|secret-sentinel' + ('|2024-06-01' if kind == 'azure_gpt' else '')
    with pytest.raises(ValueError) as error:
        azure_credential_fields(value, kind)
    assert 'secret-sentinel' not in str(error.value)


@pytest.mark.parametrize('version', [
    '', 'v1', 'preview', '2024-6-1', '20240601', '2024-06-01-preview-extra',
    '2023-02-29', '2024-13-01', '2024-04-31', '0000-01-01', '２０２４-０６-０１',
    '2024-06-01?other=1', '2024-06-01/path', '2024-06-01#fragment',
    '2024-06-01-preview/../../v1',
])
@pytest.mark.parametrize('kind', ['azure_gpt', 'azure_claude'])
def test_supplied_deployment_api_version_is_validated(version, kind):
    with pytest.raises(ValueError) as error:
        azure_credential_fields(f'resource|secret-sentinel|{version}', kind)
    assert 'secret-sentinel' not in str(error.value)


@pytest.mark.parametrize(('kind', 'value'), [
    ('azure_gpt', 'resource|secret-sentinel'),
    ('azure_gpt', 'resource||2024-06-01'),
    ('azure_gpt', 'resource|secret-sentinel|2024-06-01|extra'),
    ('azure_claude', 'secret-sentinel'),
    ('azure_claude', 'resource|'),
    ('azure_claude', 'resource|secret-sentinel|2024-06-01|extra'),
    ('azure_claude', 'resource|secret-sentinel\r\nx-api-key: injected'),
    ('azure_claude', 'resource|secret-sentinel\x00'),
    ('azure_claude', 'resource|secret-sentinel\x7f'),
    ('azure_claude', 'resource|secret sentinel'),
    ('azure_claude', 'resource|secret\tkey'),
    ('azure_claude', 'resource|secret\u200bkey'),
    ('azure_claude', 'resource|密钥'),
    ('azure_claude', ''),
    ('azure_claude', None),
    ('azure_claude', 123),
    ('azure_claude', 'resource|' + 'k' * 4096),
    ('api_key', 'resource|secret-sentinel'),
    (None, 'resource|secret-sentinel'),
    ([], 'resource|secret-sentinel'),
])
def test_invalid_credentials_fail_without_echoing_secret(kind, value):
    with pytest.raises(ValueError) as error:
        normalize_azure_credential(value, kind)
    assert 'secret-sentinel' not in str(error.value)


def test_derived_config_is_fresh_per_call():
    first = azure_credential_fields('resource|key', 'azure_claude')
    first['channel_config']['base_url'] = 'https://untrusted.example'
    first['credential_format']['remote_type'] = 3
    second = azure_credential_fields('resource|key', 'azure_claude')
    assert second['channel_config']['base_url'] == 'https://resource.services.ai.azure.com/anthropic'
    assert second['credential_format']['remote_type'] == 14


def test_optional_claude_azure_version_keeps_existing_key_identity():
    fmt = SimpleNamespace(schema_config={'type': 'azure_claude', 'remote_type': 14})
    old = 'resource|fixture-key'
    assert credential_fingerprint(old, fmt) == fingerprint(old)
    assert credential_fingerprint(old + '|2025-04-01-preview', fmt) == fingerprint(old)
    assert credential_fingerprint('other-resource|fixture-key|2025-04-01-preview', fmt) != fingerprint(old)
