"""Only mocked inference transports; no database or real provider requests."""

import base64
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, unquote

import httpx
import pytest
from app import local_model_probe as probe
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ANTHROPIC = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'OK'}]}
CHAT = {'object': 'chat.completion', 'choices': [{'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]}
RESPONSES = {'object': 'response', 'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}
GEMINI = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP'}]}
BEDROCK = {'output': {'message': {'role': 'assistant', 'content': [{'text': 'OK'}]}}, 'stopReason': 'end_turn'}


@pytest.fixture
def transport(monkeypatch):
    calls = []
    responses = []

    def send(method, url, **kwargs):
        calls.append({'method': method, 'url': url, **kwargs})
        assert responses, 'Unexpected network request'
        response = responses.pop(0)
        if callable(response):
            response = response(calls[-1])
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    monkeypatch.setattr(probe, 'safe_request', send)
    return calls, responses


def run(kind, remote_type, model, key='secret-test-key', config=None, **kwargs):
    return probe.probe_credential(key, {'type': kind, 'remote_type': remote_type}, model, config or {}, **kwargs)


def body(call):
    return json.loads(call['data'])


def sent_content(call):
    payload = body(call)
    if 'input' in payload:
        return payload['input']
    if 'contents' in payload:
        return payload['contents'][0]['parts'][0]['text']
    content = payload['messages'][0]['content']
    return content[0]['text'] if isinstance(content, list) else content


@pytest.mark.parametrize('content', [None, '自定义测试内容\n"请回答 OK" 🟢'])
@pytest.mark.parametrize(('kind', 'number', 'model', 'key', 'config', 'response', 'url', 'header'), [
    ('api_key', 14, 'claude-sonnet-4-6', 'test-ant', {}, ANTHROPIC, 'https://api.anthropic.com/v1/messages', 'x-api-key'),
    ('api_key', 1, 'gpt-4o-mini', 'test-oai', {}, CHAT, 'https://api.openai.com/v1/chat/completions', 'Authorization'),
    ('api_key', 1, 'gpt-5.6-sol', 'test-oai', {}, RESPONSES, 'https://api.openai.com/v1/responses', 'Authorization'),
    ('api_key', 20, 'claude-sonnet-4-6', 'test-or', {'model_mapping': {'claude-sonnet-4-6': 'anthropic/claude-sonnet-4.6'}}, CHAT, 'https://openrouter.ai/api/v1/chat/completions', 'Authorization'),
    ('api_key', 14, 'claude-haiku-4-5-20251001', 'test-oc', {'base_url': 'https://opencode.ai/zen', 'model_mapping': {'claude-haiku-4-5-20251001': 'claude-haiku-4-5'}}, ANTHROPIC, 'https://opencode.ai/zen/v1/messages', 'x-api-key'),
    ('aws_claude', 14, 'claude-opus-4-8', 'test-aws', {'base_url': 'https://real-resource.api.aws'}, ANTHROPIC, 'https://real-resource.api.aws/v1/messages', 'x-api-key'),
    ('azure_gpt', 3, 'gpt-4o', 'resource-a|test-az|2025-04-01-preview', {}, CHAT, 'https://resource-a.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2025-04-01-preview', 'api-key'),
    ('azure_gpt', 3, 'gpt-5.3-codex', 'resource-a|test-az|2025-04-01-preview', {}, RESPONSES, 'https://resource-a.openai.azure.com/openai/v1/responses', 'api-key'),
    ('azure_claude', 14, 'claude-opus-4-8', 'resource-b|test-az|2025-04-01-preview', {}, ANTHROPIC, 'https://resource-b.services.ai.azure.com/anthropic/v1/messages', 'x-api-key'),
    ('api_key', 24, 'gemini-3.8-flash', 'test-google', {}, GEMINI, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent', 'x-goog-api-key'),
    ('vertex_gemini', 41, 'gemini-3.8-flash', 'test-google', {}, GEMINI, 'https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.8-flash:generateContent', 'x-goog-api-key'),
    ('vertex_claude', 41, 'claude-haiku-4-5-20251001', 'test-google', {}, ANTHROPIC, 'https://aiplatform.googleapis.com/v1/publishers/anthropic/models/claude-haiku-4-5%4020251001:rawPredict', 'x-goog-api-key'),
])
def test_provider_inference_contract(transport, kind, number, model, key, config, response, url, header, content):
    calls, responses = transport
    responses.append(response)
    guards = []
    result = run(kind, number, model, key, config, before_request=lambda: guards.append('guard'), content=content)
    assert result['success'] is True
    assert result['http_status'] == 200
    assert result['provider_message'] == 'OK'
    assert len(calls) == len(guards) == 1
    call = calls[0]
    assert call['method'] == 'POST' and call['url'] == url
    assert header in call['headers']
    assert call['timeout'] == 35
    assert sent_content(call) == (content or probe._PROMPT)
    assert key not in json.dumps(result)
    assert content is None or content not in str(result)
    if kind.startswith('azure_'):
        assert call['headers'][header] == 'test-az'
        assert 'resource-' not in call['headers'][header]
        if kind == 'azure_claude':
            assert call['headers']['anthropic-version'] == '2023-06-01'
            assert '2025-04-01' not in call['url'] and '2025-04-01' not in call['data'].decode()
    if number == 20:
        assert body(call)['model'] == 'anthropic/claude-sonnet-4.6'
    if kind == 'vertex_claude':
        assert 'model' not in body(call)
        assert body(call)['anthropic_version'] == 'vertex-2023-10-16'


@pytest.mark.parametrize('content', [None, '签名包含实际发送内容\n"你好" 🟢'])
@pytest.mark.parametrize(('key', 'kind', 'region', 'authorization'), [
    ('AKIAIOSFODNN7EXAMPLE|example-secret|us-east-1', 'aws_bedrock', 'us-east-1', 'AWS4-HMAC-SHA256'),
    ('test-bedrock-key|eu-west-1', 'aws_bedrock', 'eu-west-1', 'Bearer test-bedrock-key'),
    ('test-bedrock-key|ap-northeast-1', 'aws_api_key', 'ap-northeast-1', 'Bearer test-bedrock-key'),
])
def test_bedrock_signed_or_bearer_converse(transport, key, kind, region, authorization, content):
    calls, responses = transport
    responses.append(BEDROCK)
    result = run(kind, 33, 'claude-sonnet-4-6', key, content=content)
    assert result['success'] and result['http_status'] == 200
    call = calls[0]
    area = 'apac' if region.startswith('ap-') else region[:2]
    assert unquote(call['url']) == f'https://bedrock-runtime.{region}.amazonaws.com/model/{area}.anthropic.claude-sonnet-4-6/converse'
    assert call['headers']['Authorization'].startswith(authorization)
    assert body(call)['inferenceConfig']['maxTokens'] == 8
    assert sent_content(call) == (content or probe._PROMPT)
    if authorization == 'AWS4-HMAC-SHA256':
        stamp = datetime.strptime(call['headers']['X-Amz-Date'], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
        expected = probe._sigv4(call['url'], call['data'], key.split('|')[0], key.split('|')[1], region, stamp)
        assert call['headers']['Authorization'] == expected['Authorization']
    assert key not in call['data'].decode()


def test_bedrock_explicit_model_mapping_and_unknown_are_not_guessed(transport):
    calls, responses = transport
    responses.extend([BEDROCK, httpx.Response(404, json={'error': 'secret-model'})])
    config = {'model_mapping': {'local-alias': 'global.anthropic.claude-opus-4-8'}}
    assert run('aws_api_key', 33, 'local-alias', 'test-key|us-east-1', config)['success']
    assert '/model/global.anthropic.claude-opus-4-8/converse' in calls[0]['url']
    result = run('aws_api_key', 33, 'claude-unknown-new', 'test-key|us-east-1')
    assert result['error_code'] == 'model_unavailable'
    assert '/model/claude-unknown-new/converse' in calls[1]['url']
    assert result['provider_message'] == 'secret-model'
    assert 'test-key' not in json.dumps(result)


def test_sigv4_is_deterministic_binds_body_region_and_escaped_path():
    stamp = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    url = 'https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-v1%3A0/converse'
    headers = probe._sigv4(url, b'{"test":1}', 'EXAMPLEACCESS', 'EXAMPLESECRET', 'us-east-1', stamp)
    assert headers['X-Amz-Date'] == '20260909T000000Z'
    assert '20260909/us-east-1/bedrock/aws4_request' in headers['Authorization']
    assert headers == probe._sigv4(url, b'{"test":1}', 'EXAMPLEACCESS', 'EXAMPLESECRET', 'us-east-1', stamp)
    for changed in (probe._sigv4(url, b'{"test":2}', 'EXAMPLEACCESS', 'EXAMPLESECRET', 'us-east-1', stamp),
                    probe._sigv4(url.replace('%3A0', '%3A1'), b'{"test":1}', 'EXAMPLEACCESS', 'EXAMPLESECRET', 'us-east-1', stamp)):
        assert changed['Authorization'] != headers['Authorization']
    assert 'EXAMPLESECRET' not in str(headers)


@pytest.fixture
def account():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    value = {'type': 'service_account', 'project_id': 'project-example', 'client_email': 'probe@project-example.iam.gserviceaccount.com',
             'private_key': private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
             'token_uri': 'https://must-not-contact.invalid/token'}
    return private, value


@pytest.mark.parametrize('content', [None, 'Vertex 的自定义消息'])
@pytest.mark.parametrize(('kind', 'model', 'region', 'response', 'publisher', 'suffix'), [
    ('vertex_gemini', 'gemini-3.8-flash', 'global', GEMINI, 'google', 'generateContent'),
    ('vertex_claude', 'claude-sonnet-4-6', 'us-east5', ANTHROPIC, 'anthropic', 'rawPredict'),
])
def test_vertex_service_account_oauth_and_inference_both_guarded(transport, account, kind, model, region, response, publisher, suffix, content):
    private, value = account
    calls, responses = transport
    responses.extend([{'access_token': 'short-lived-google-token', 'token_type': 'Bearer'}, response])
    guarded = []
    result = run(kind, 41, model, json.dumps(value), {'other': json.dumps({'default': 'global', model: region})}, before_request=lambda: guarded.append(len(calls)), content=content)
    assert result['success'] is True and guarded == [0, 1]
    token_call, inference = calls
    assert token_call['url'] == 'https://oauth2.googleapis.com/token'
    assert token_call['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    form = parse_qs(token_call['data'].decode())
    assert form['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
    jwt = form['assertion'][0].split('.')
    decode = lambda segment: base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4))
    private.public_key().verify(decode(jwt[2]), '.'.join(jwt[:2]).encode(), padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(decode(jwt[1]))
    assert claims['aud'] == token_call['url']
    assert claims['scope'] == 'https://www.googleapis.com/auth/cloud-platform'
    assert claims['exp'] - claims['iat'] == 300
    assert 'sub' not in claims
    host = 'aiplatform.googleapis.com' if region == 'global' else region + '-aiplatform.googleapis.com'
    assert inference['url'] == f'https://{host}/v1/projects/project-example/locations/{region}/publishers/{publisher}/models/{model}:{suffix}'
    assert inference['headers']['Authorization'] == 'Bearer short-lived-google-token'
    assert sent_content(inference) == (content or probe._PROMPT)
    assert value['private_key'] not in str(result) and 'short-lived-google-token' not in str(result)


@pytest.mark.parametrize('guard_error', [RuntimeError('cancelled'), ValueError('lease'), httpx.ConnectError('removed')])
def test_guard_exception_propagates_unchanged_and_prevents_network(transport, guard_error):
    calls, _ = transport

    def stop():
        raise guard_error

    with pytest.raises(type(guard_error)) as raised:
        run('api_key', 14, 'claude-sonnet-4-6', before_request=stop)
    assert raised.value is guard_error
    assert calls == []


def test_cancellation_between_oauth_and_inference_never_sends_inference(transport, account):
    calls, responses = transport
    responses.append({'access_token': 'short-token', 'token_type': 'Bearer'})
    removed = RuntimeError('distribution hard-deleted')

    def stop_second():
        if calls:
            raise removed

    with pytest.raises(RuntimeError) as raised:
        run('vertex_gemini', 41, 'gemini-3.8-flash', json.dumps(account[1]), before_request=stop_second)
    assert raised.value is removed and len(calls) == 1


@pytest.mark.parametrize(('remote', 'expected'), [
    (httpx.Response(302, headers={'location': 'https://must-not-contact.invalid'}), 'redirect_rejected'),
    (httpx.Response(401, json={'error': {'message': 'secret-test-key'}}), 'authentication_failed'),
    (httpx.Response(403, text='secret-test-key'), 'authentication_failed'),
    (httpx.Response(404, text='secret-test-key'), 'model_unavailable'),
    (httpx.Response(429, json={'secret': 'secret-test-key'}), 'rate_limited'),
    (httpx.Response(400, text='custom-private-content'), 'provider_error'),
    (httpx.Response(500, text='secret-test-key'), 'provider_error'),
    (httpx.Response(503, text='custom-private-content'), 'provider_error'),
    (httpx.Response(200, text='<html>logged in</html>'), 'invalid_response'),
    ({'success': True, 'models': ['claude-sonnet-4-6']}, 'invalid_response'),
    ({'error': {'message': 'secret-test-key'}}, 'invalid_response'),
    (httpx.ReadTimeout('secret-test-key'), 'timeout'),
    (httpx.ConnectError('secret-test-key'), 'connection_error'),
])
def test_remote_failure_is_safe_and_never_retried(transport, remote, expected):
    calls, responses = transport
    responses.append(remote)
    result = run('api_key', 14, 'claude-sonnet-4-6', content='custom-private-content')
    assert result['success'] is False and result['error_code'] == expected
    assert result['message'] == probe.ERROR_MESSAGES[expected]
    assert result['http_status'] == (None if isinstance(remote, Exception)
                                      else remote.status_code if isinstance(remote, httpx.Response) else 200)
    assert 'secret-test-key' not in json.dumps(result)
    assert 'provider_message' in result
    assert len(calls) == 1


@pytest.mark.parametrize(('config', 'proxy'), [({}, 'http://proxy.invalid'), ({'proxy': 'http://proxy.invalid'}, ''), ({'setting': '{"proxy":"socks5://proxy.invalid"}'}, '')])
def test_proxy_never_silently_ignored(transport, config, proxy):
    calls, _ = transport
    assert run('api_key', 1, 'gpt-4o', config=config, proxy=proxy)['error_code'] == 'proxy_unsupported'
    assert calls == []


@pytest.mark.parametrize(('kind', 'number', 'key', 'config'), [
    ('azure_gpt', 3, 'https://bad.invalid|secret|2025-01-01', {}),
    ('aws_claude', 14, 'test-key', {'base_url': 'https://bad.invalid'}),
    ('api_key', 1, 'test-key', {'base_url': 'https://bad.invalid'}),
    ('api_key', 14, 'test-key', {'base_url': 'http://api.anthropic.com'}),
    ('api_key', 14, 'test-key', {'model_mapping': {'model': {'key': 'test-key'}}}),
    ('vertex_gemini', 41, 'test-key', {'other': '{"default":"../escape"}'}),
])
def test_invalid_configuration_never_sends_credential(transport, kind, number, key, config):
    calls, _ = transport
    assert run(kind, number, 'model', key, config)['success'] is False
    assert calls == []


def test_vertex_bad_private_key_does_not_contact_oauth(transport, account):
    calls, _ = transport
    account[1]['private_key'] = 'not a PEM key'
    assert run('vertex_claude', 41, 'claude-opus-4-8', json.dumps(account[1]))['error_code'] == 'invalid_credential'
    assert calls == []


def test_embeddings_and_budget_exhaustion_are_actual_protocol_results(transport):
    calls, responses = transport
    responses.extend([{'object': 'list', 'data': [{'embedding': [0.25, -0.5]}]},
                      {'object': 'response', 'status': 'incomplete', 'output': [], 'incomplete_details': {'reason': 'max_output_tokens'}, 'usage': {'output_tokens': 16}}])
    assert run('api_key', 1, 'text-embedding-3-small')['success']
    assert run('api_key', 1, 'gpt-5.6-sol')['success']
    assert calls[0]['url'].endswith('/embeddings')
    assert body(calls[1])['max_output_tokens'] == 16


def test_gemini_small_budget_can_finish_during_thinking(transport):
    calls, responses = transport
    responses.extend([{'candidates': [{'finishReason': 'MAX_TOKENS'}], 'usageMetadata': {'thoughtsTokenCount': 8}},
                      {'candidates': [{'finishReason': 'MAX_TOKENS'}], 'usageMetadata': {'promptTokenCount': 8}}])
    assert run('api_key', 24, 'gemini-2.5-pro')['success']
    assert run('api_key', 24, 'gemini-2.5-pro')['error_code'] == 'invalid_response'
    assert len(calls) == 2


@pytest.mark.parametrize('model', ['gpt-image-1', 'whisper-1', 'gpt-realtime', 'tts-1'])
def test_special_media_model_is_not_reported_connected(transport, model):
    calls, _ = transport
    assert run('api_key', 1, model)['error_code'] == 'unsupported_model_kind'
    assert calls == []


@pytest.mark.parametrize('content', [123, True, [], {}, 'x' * 1001, '\ud800'])
def test_invalid_custom_content_stops_before_guard_or_network(transport, content):
    calls, _ = transport
    guards = []
    result = run('api_key', 1, 'gpt-4o', content=content, before_request=lambda: guards.append(True))
    assert result['error_code'] == 'invalid_test_content' and result['http_status'] is None
    assert not calls and not guards


@pytest.mark.parametrize('content', ['', ' \n\t', '界' * 1000])
def test_custom_content_default_and_length_boundary(transport, content):
    calls, responses = transport
    responses.append(CHAT)
    assert run('api_key', 1, 'gpt-4o', content=content)['success']
    assert sent_content(calls[0]) == (content if content.strip() else probe._PROMPT)


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_oauth_http_failure_is_reported_without_inference_or_secrets(transport, account, status):
    calls, responses = transport
    responses.append(httpx.Response(status, text='PRIVATE TOKEN AND CONTENT'))
    result = run('vertex_gemini', 41, 'gemini-3.8-flash', json.dumps(account[1]), content='PRIVATE CONTENT')
    assert result['success'] is False and result['http_status'] == status
    assert len(calls) == 1 and calls[0]['url'] == probe._TOKEN_URL
    assert result['provider_message'] == 'PRIVATE TOKEN AND CONTENT'
    assert account[1]['private_key'] not in str(result)


def test_http_status_comes_from_response_and_not_payload(transport):
    calls, responses = transport
    responses.extend([httpx.Response(201, json={**ANTHROPIC, 'http_status': 401}),
                      httpx.Response(200, json={'error': {'http_status': 503, 'message': 'private'}})])
    assert run('api_key', 14, 'claude-sonnet-4-6')['http_status'] == 201
    failed = run('api_key', 14, 'claude-sonnet-4-6')
    assert failed['http_status'] == 200 and failed['error_code'] == 'invalid_response'
    assert failed['provider_message'] == 'private' and len(calls) == 2


@pytest.mark.parametrize('payload', [
    {'error': {'message': 'Your account has exhausted its quota.'}},
    {'error': {'detail': 'Your account has exhausted its quota.'}},
    {'message': 'Your account has exhausted its quota.'},
    {'Message': 'Your account has exhausted its quota.', '__type': 'AccessDeniedException'},
    {'detail': 'Your account has exhausted its quota.'},
    {'error_description': 'Your account has exhausted its quota.', 'error': 'invalid_grant'},
    {'error': 'Your account has exhausted its quota.'},
])
def test_provider_error_text_is_preserved_from_only_supported_message_fields(transport, payload):
    calls, responses = transport
    responses.append(httpx.Response(429, json={**payload, 'debug': {'body': 'must-not-copy'}, 'request': 'must-not-copy'}))
    result = run('api_key', 14, 'claude-sonnet-4-6')
    assert result['provider_message'] == 'Your account has exhausted its quota.'
    assert result['message'] == probe.ERROR_MESSAGES['rate_limited']
    assert 'must-not-copy' not in str(result) and len(calls) == 1


@pytest.mark.parametrize(('text', 'expected'), [
    ('Model access denied\nContact the administrator.', 'Model access denied Contact the administrator.'),
    (('<html><head><style>private-css</style></head><body><h1>Access denied</h1><p>Try again.</p>'
      '<script>private-script</script><!--private-comment--></body></html>'), 'Access denied Try again.'),
    ('Bad\x00 request\u202e \tcheck\x7f permission', 'Bad request check permission'),
])
def test_plain_text_html_and_controls_produce_readable_message(transport, text, expected):
    _, responses = transport
    responses.append(httpx.Response(403, text=text))
    result = run('api_key', 14, 'claude-sonnet-4-6')
    assert result['provider_message'] == expected
    assert '<' not in result['provider_message'] and 'private-' not in str(result)


@pytest.mark.parametrize('http_status', [200, 401])
def test_echoed_complete_and_encoded_api_key_is_redacted_before_return(transport, http_status):
    _, responses = transport
    key = 'sk-secret+/encoded=value'
    responses.append(httpx.Response(http_status, json={'error': {'message':
        f'Invalid API key {key}, encoded={quote(key, safe="")}. Model unavailable.'}}))
    result = run('api_key', 14, 'claude-sonnet-4-6', key)
    assert result['provider_message'] == 'Invalid API key [REDACTED], encoded=[REDACTED]. Model unavailable.'
    assert result['http_status'] == http_status and result['success'] is False


def test_aws_echo_masks_both_key_parts_and_generated_signature_but_keeps_region(transport):
    _, responses = transport
    key = 'AKIAIOSFODNN7EXAMPLE|aws-private-secret|us-east-1'
    secrets = []

    def failure(call):
        authorization = call['headers']['Authorization']
        signature = authorization.split('Signature=')[1]
        secrets.extend([*key.split('|')[:2], authorization, signature])
        return httpx.Response(403, json={'Message': f'AccessDenied in us-east-1: {key}; {authorization}; {signature}'})

    responses.append(failure)
    result = run('aws_bedrock', 33, 'claude-sonnet-4-6', key)
    assert 'AccessDenied in us-east-1:' in result['provider_message']
    assert all(value not in str(result) for value in secrets)


def test_azure_echo_keeps_resource_and_version_but_masks_actual_key(transport):
    _, responses = transport
    responses.append(httpx.Response(401, json={'message': 'resource-a 2025-04-01-preview rejected azure-private-key'}))
    result = run('azure_gpt', 3, 'gpt-4o', 'resource-a|azure-private-key|2025-04-01-preview')
    assert result['provider_message'] == 'resource-a 2025-04-01-preview rejected [REDACTED]'


@pytest.mark.parametrize('success', [False, True])
def test_oauth_assertion_private_key_and_access_token_echoes_are_redacted(transport, account, success):
    calls, responses = transport
    _, account_value = account
    assertion_values = []

    def oauth(call):
        assertion = parse_qs(call['data'].decode())['assertion'][0]
        assertion_values.append(assertion)
        if success:
            return {'access_token': 'google-private-token', 'token_type': 'Bearer', 'message': 'oauth-message-never-displayed'}
        return httpx.Response(401, json={'access_token': 'google-private-token',
            'error_description': 'Invalid grant: ' + assertion + ' ' + quote(assertion, safe='') + ' '
                + account_value['private_key'] + ' google-private-token'})

    responses.append(oauth)
    if success:
        def inference(call):
            text = 'Hello. ' + call['headers']['Authorization'] + ' ' + assertion_values[0] + ' ' + account_value['private_key']
            return {**GEMINI, 'candidates': [{'content': {'parts': [{'text': text}]}}]}
        responses.append(inference)
    result = run('vertex_gemini', 41, 'gemini-3.8-flash', json.dumps(account_value))
    assert result['success'] is success
    assert len(calls) == (2 if success else 1)
    for secret in [assertion_values[0], assertion_values[0].split('.')[-1], 'google-private-token',
                   account_value['private_key'], *account_value['private_key'].splitlines()[1:-1]]:
        assert secret not in str(result)
    assert 'oauth-message-never-displayed' not in str(result)
    assert '[REDACTED]' in result['provider_message']


def test_request_url_query_and_authorization_secrets_are_not_echoed(transport):
    _, responses = transport
    url = 'https://example.invalid/models?key=query-private-token&access_token=query-second-token'
    responses.append(httpx.Response(403, json={'message': url + ' Bearer auth-private-token'}))
    with pytest.raises(probe._ProbeFailure) as raised:
        probe._request(url, {'Authorization': 'Bearer auth-private-token'}, b'{}', None, set())
    message = raised.value.provider_message
    assert 'query-private-token' not in message and 'query-second-token' not in message
    assert 'auth-private-token' not in message and 'example.invalid' in message


def test_long_secret_is_redacted_before_shortening_the_message(transport):
    _, responses = transport
    key = 'sk-' + 'PRIVATE-LONG-KEY-' * 150
    responses.append(httpx.Response(401, json={'message': 'Rejected ' + key + ' ' + 'details ' * 400}))
    result = run('api_key', 14, 'claude-sonnet-4-6', key)
    message = result['provider_message']
    assert len(message) == 1500 and message.endswith('…')
    assert 'PRIVATE-LONG-KEY' not in message and message.startswith('Rejected [REDACTED]')


def test_unrecognized_payload_and_excessive_body_are_not_copied(transport):
    _, responses = transport
    responses.extend([httpx.Response(500, json={'debug': {'message': 'not a public message'}, 'headers': {'secret': 'do-not-copy'}}),
                      httpx.Response(500, text='x' * (probe._RESPONSE_LIMIT + 1))])
    for _ in range(2):
        assert run('api_key', 14, 'claude-sonnet-4-6')['provider_message'] is None


def test_no_text_protocol_does_not_invent_provider_message(transport):
    _, responses = transport
    responses.extend([{'data': [{'embedding': [0.25, -0.5]}]},
                      {'results': [{'flagged': False, 'categories': {}}]}])
    assert run('api_key', 1, 'text-embedding-3-small')['provider_message'] is None
    assert run('api_key', 1, 'omni-moderation-latest')['provider_message'] is None


def test_large_valid_embedding_payload_keeps_protocol_success(transport):
    _, responses = transport
    value = {'data': [{'embedding': [0.123456789] * 10000}]}
    assert len(json.dumps(value)) > probe._RESPONSE_LIMIT
    responses.append(value)
    result = run('api_key', 1, 'text-embedding-3-small')
    assert result['success'] and result['http_status'] == 200 and result['provider_message'] is None


@pytest.mark.parametrize(('kind', 'number', 'model', 'value'), [
    ('api_key', 24, 'gemini-3.8-flash', {'candidates': [GEMINI['candidates'][0], {'content': 'malformed'}, None]}),
    ('api_key', 1, 'gpt-4o', {'choices': [CHAT['choices'][0], {'message': 'malformed'}, {'message': {'role': 'user', 'content': 'do-not-copy'}}]}),
    ('api_key', 1, 'gpt-5.6-sol', {**RESPONSES, 'output': [*RESPONSES['output'], None, {'type': 'message', 'content': False}]}),
])
def test_success_text_extraction_tolerates_other_malformed_candidates(transport, kind, number, model, value):
    _, responses = transport
    responses.append(value)
    result = run(kind, number, model)
    assert result['success'] and result['provider_message'] == 'OK'


@pytest.mark.parametrize('success', [False, True])
def test_json_message_angle_brackets_remain_provider_plain_text(transport, success):
    _, responses = transport
    message = 'Model <claude-opus-4-8> is not enabled. <script>literal</script>'
    response = {**ANTHROPIC, 'content': [{'type': 'text', 'text': message}]} if success else {'error': {'message': message}}
    responses.append(httpx.Response(200 if success else 404, json=response))
    result = run('api_key', 14, 'claude-sonnet-4-6')
    assert result['success'] is success and result['provider_message'] == message


def test_error_type_fallback_does_not_treat_success_metadata_as_message(transport):
    _, responses = transport
    responses.extend([httpx.Response(400, json={'error': {'type': 'invalid_request_error'}}),
                      {'object': 'list', 'data': [{'embedding': [0.1]}]}])
    assert run('api_key', 14, 'claude-sonnet-4-6')['provider_message'] == 'invalid_request_error'
    assert run('api_key', 1, 'text-embedding-3-small')['provider_message'] is None


def test_session_tokens_from_credential_fields_are_redacted():
    secrets = set()
    probe._remember_fields(secrets, {'sessionToken': 'session-private-value', 'region': 'us-east-1'})
    assert probe._provider_message('session-private-value cannot access us-east-1', secrets) == '[REDACTED] cannot access us-east-1'
