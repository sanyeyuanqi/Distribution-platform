"""Local parsing of the official AWS short-term token envelope, never network validation."""
import base64
from urllib.parse import urlencode

import pytest
from app.newapi_formats import bedrock_token_region, normalize_bedrock_credential


def token_with(updates=None, *, host='bedrock.amazonaws.com', duplicate=False):
    query = {
        'Action': 'CallWithBearerToken', 'Version': '1',
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': 'ASIA' + 'A' * 16 + '/20260908/eu-west-1/bedrock/aws4_request',
        'X-Amz-Date': '20260908T010203Z', 'X-Amz-Expires': '43200',
        'X-Amz-SignedHeaders': 'host', 'X-Amz-Signature': 'a' * 64,
        'X-Amz-Security-Token': 'private-fixture-session',
    }
    query.update(updates or {})
    url = host + '/?' + urlencode(query)
    if duplicate:
        url += '&Version=1'
    return 'bedrock-api-key-' + base64.b64encode(url.encode()).decode()


def test_signed_token_uses_embedded_region_and_preserves_key_bytes():
    token = token_with()
    assert bedrock_token_region(token) == 'eu-west-1'
    assert normalize_bedrock_credential(token) == token + '|eu-west-1'
    assert normalize_bedrock_credential(token + '|eu-west-1') == token + '|eu-west-1'
    with pytest.raises(ValueError, match='签名地区不一致'):
        normalize_bedrock_credential(token + '|us-east-1')


@pytest.mark.parametrize('updates', [
    {'Action': 'DifferentAction'}, {'Version': '2'}, {'X-Amz-Algorithm': 'none'},
    {'X-Amz-Credential': 'ASIA' + 'A' * 16 + '/20260908/us-east-1/sts/aws4_request'},
    {'X-Amz-Credential': 'ASIA' + 'A' * 16 + '/20260908/invalid/bedrock/aws4_request'},
    {'X-Amz-Date': '20260909T010203Z'}, {'X-Amz-Date': '20260908T250203Z'},
    {'X-Amz-SignedHeaders': 'host;custom'}, {'X-Amz-Signature': 'private-fixture'},
    {'X-Amz-Expires': '0'}, {'X-Amz-Expires': '43201'}, {'X-Amz-Expires': '-1'},
    {'X-Amz-Security-Token': ''},
])
def test_recognized_but_malformed_token_does_not_fall_back_or_leak(updates):
    token = token_with(updates)
    with pytest.raises(ValueError, match='临时 Bedrock API Key 格式无效') as caught:
        normalize_bedrock_credential(token)
    assert 'private-fixture' not in str(caught.value) and token not in str(caught.value)


@pytest.mark.parametrize('token', [
    'bedrock-api-key-invalid!base64',
    'bedrock-api-key-' + base64.b64encode(b'https://bedrock.amazonaws.com/?Version=1').decode(),
    token_with(host='private-fixture.example'),
    token_with(host='bedrock.amazonaws.com:443'),
    token_with(host='user@bedrock.amazonaws.com'),
    token_with(duplicate=True),
    token_with() + '=',
])
def test_token_envelope_rejects_ambiguous_hosts_duplicates_and_encoding(token):
    with pytest.raises(ValueError):
        bedrock_token_region(token)


def test_non_token_keys_use_default_and_explicit_region_is_unchanged():
    assert bedrock_token_region('ABSK-private-fixture') is None
    assert normalize_bedrock_credential('ABSK-private-fixture') == 'ABSK-private-fixture|us-east-1'
    assert normalize_bedrock_credential('ABSK-private-fixture|ap-northeast-1') == 'ABSK-private-fixture|ap-northeast-1'


def test_completed_wire_key_still_respects_existing_adapter_length_limit():
    assert len(normalize_bedrock_credential('a' * 4086)) == 4096
    with pytest.raises(ValueError, match='4096'):
        normalize_bedrock_credential('a' * 4087)
