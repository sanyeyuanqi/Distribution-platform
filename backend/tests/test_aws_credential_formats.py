"""Pure AWS input contracts; no database fixtures or network requests."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.newapi_formats import (
    credential_records,
    credential_wire_schema,
    format_spec,
    get_format_specs,
    normalize_credential,
    validate_aws_claude_base_url,
)


def aws_format(kind):
    spec = next(item for item in get_format_specs() if item['schema_config']['type'] == kind)
    return SimpleNamespace(**spec)


@pytest.mark.parametrize(('kind', 'code', 'remote_type'), [
    ('aws_bedrock', 'newapi-33-aws-bedrock-v1', 33),
    ('aws_claude', 'newapi-14-aws-claude-v1', 14),
])
def test_new_aws_formats_are_canonical_and_reject_changed_protocol(kind, code, remote_type):
    candidate = aws_format(kind)
    spec = format_spec(candidate)
    assert spec['family'] == 'AWS'
    assert spec['code'] == code
    assert spec['version'] == '1'
    assert spec['remote_type'] == remote_type
    assert not spec['json']
    assert spec['schema_config'] == {'type': kind, 'remote_type': remote_type}

    candidate.schema_config = {'type': kind, 'remote_type': 1}
    assert format_spec(candidate) is None
    with pytest.raises(ValueError, match='凭据格式未实现'):
        normalize_credential('private-test-token', candidate)


def test_merged_bedrock_batch_keeps_each_record_and_chooses_its_wire_mode():
    candidate = aws_format('aws_bedrock')
    raw = '\r\n AKIA_TEST|private-test-secret|us-east-1 \r\nprivate-test-api|eu-west-1\r\n'
    values, line_numbers = credential_records(raw, candidate)
    normalized = [normalize_credential(value, candidate) for value in values]
    assert line_numbers == [2, 3]
    assert normalized == ['AKIA_TEST|private-test-secret|us-east-1', 'private-test-api|eu-west-1']
    assert [credential_wire_schema(value, candidate.schema_config) for value in normalized] == [
        {'type': 'aws_ak_sk', 'remote_type': 33},
        {'type': 'aws_api_key', 'remote_type': 33},
    ]
    assert candidate.schema_config == {'type': 'aws_bedrock', 'remote_type': 33}


@pytest.mark.parametrize('value', [
    'AKIA_TEST|private-test-secret|us-gov-west-1',
    'private-test-api|cn-northwest-1',
])
def test_merged_bedrock_accepts_explicit_partition_regions(value):
    assert normalize_credential(value, aws_format('aws_bedrock')) == value


@pytest.mark.parametrize('value', [
    '', 'AKIA_TEST|private-test-secret',
    'AKIA_TEST||us-east-1', '|us-east-1', 'private-test-api|',
    'AKIA_TEST|private-test-secret|us-east-1|extra',
    'private-test-api|useast1', 'private-test-api|US-EAST-1',
    'private-test-api|us-east', 'private-test-api|us-east-1/extra',
    'private-test-api| us-east-1', 'private-test-api\n|us-east-1',
])
def test_merged_bedrock_rejects_missing_or_malformed_fields_without_echo(value):
    with pytest.raises(ValueError) as result:
        normalize_credential(value, aws_format('aws_bedrock'))
    assert 'private-test' not in str(result.value)


@pytest.mark.parametrize('value', ['token', 'token|', '|us-east-1', 'ak|sk|us-east-1|extra'])
def test_wire_schema_rejects_unvalidated_malformed_bedrock_records(value):
    with pytest.raises(ValueError):
        credential_wire_schema(value, {'type': 'aws_bedrock', 'remote_type': 33})


def test_wire_schema_preserves_other_protocols_without_sharing_mutable_state():
    schema = {'type': 'aws_claude', 'remote_type': 14, 'nested': {'example': True}}
    original = deepcopy(schema)
    result = credential_wire_schema('private-test-token', schema)
    assert result == original
    result['nested']['example'] = False
    assert schema == original
    assert credential_wire_schema('private-test-token', None) is None


def test_claude_proxy_only_accepts_key_in_credential_field():
    candidate = aws_format('aws_claude')
    assert normalize_credential('  private-test-token\n', candidate) == 'private-test-token'
    assert credential_wire_schema('private-test-token', candidate.schema_config) == {
        'type': 'aws_claude', 'remote_type': 14,
    }
    for value in ('private-test-token,https://company-a.api.aws', 'private-test-token|us-east-1'):
        with pytest.raises(ValueError, match='API 地址请在单独的地址栏填写') as result:
            normalize_credential(value, candidate)
        assert 'private-test-token' not in str(result.value)


@pytest.mark.parametrize(('value', 'expected'), [
    ('https://company-a.api.aws', 'https://company-a.api.aws'),
    ('  https://COMPANY-A.API.AWS/  ', 'https://company-a.api.aws'),
    ('https://edge.company-a.api.aws/', 'https://edge.company-a.api.aws'),
])
def test_proxy_origin_normalization(value, expected):
    assert validate_aws_claude_base_url(value) == expected


@pytest.mark.parametrize('value', [
    '', 'http://company-a.api.aws', 'https://api.aws',
    'https://company-a.example.com', 'https://company-a.api.aws.example.com',
    'https://company-a.api.aws:443', 'https://company-a.api.aws:8443',
    'https://company-a.api.aws:notaport',
    'https://private-user:private-password@company-a.api.aws',
    'https://company-a.api.aws/v1', 'https://company-a.api.aws//',
    'https://company-a.api.aws?token=private-test-token',
    'https://company-a.api.aws/#private-test-token',
    'https://company-a .api.aws', 'https://company-a.api.aws\n/private-test-token',
    'https://company_a.api.aws', 'https://-company.api.aws', 'https://company-.api.aws',
    'https://company-a..api.aws', 'https://company-a.api.aws.',
    'https://xxx.api.aws', 'https://example.api.aws', 'https://test.api.aws',
    'https://tenant.api.aws', 'https://your-tenant.api.aws', 'https://placeholder.api.aws',
    'https://bedrock-mantle.us-east-1.api.aws',
    'https://aws-external-anthropic.us-west-2.api.aws',
])
def test_proxy_rejects_unsafe_ambiguous_and_different_protocol_origins(value):
    with pytest.raises(ValueError) as result:
        validate_aws_claude_base_url(value)
    error = str(result.value)
    assert 'private-user' not in error
    assert 'private-password' not in error
    assert 'private-test-token' not in error
