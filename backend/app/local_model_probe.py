"""One bounded inference from one saved credential, without a receiving site.

Contracts: OpenAI Responses/Chat, Anthropic Messages, Bedrock Converse,
Google generateContent/Vertex rawPredict, Microsoft Foundry and OpenCode Zen.
No discovery request, automatic retry, redirect, or ambient proxy is used.
The authorization callback deliberately runs outside all exception handlers.
"""

import base64
import hashlib
import hmac
import json
import math
import re
import time
import unicodedata
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .azure_credentials import azure_credential_fields
from .network import safe_request
from .newapi_formats import (
    credential_wire_schema,
    load_credential_json,
    normalize_credential,
    validate_aws_claude_base_url,
)

_PROMPT = 'Reply OK.'
_TOKEN_URL = 'https://oauth2.googleapis.com/token'
ERROR_MESSAGES = {
    'invalid_test_content': '测试内容须为不超过 1000 个字符的有效文本',
    'invalid_credential': '凭据格式无效，无法在本地发起测试',
    'invalid_configuration': '此渠道的本地测试配置无效，请检查资源、地区和模型映射',
    'unsupported_protocol': '此凭据类型暂不支持本地模型测试',
    'unsupported_model_kind': '此模型需要专用音视频或图像测试，暂不支持轻量文本测试',
    'proxy_unsupported': '此渠道配置了代理，本地测试暂不支持该代理，未发起直连请求',
    'connection_error': '本地后台无法连接模型服务，请检查网络后重新测试',
    'timeout': '模型服务请求超时，结果未知，请勿自动重试',
    'authentication_failed': '模型服务拒绝认证或访问，请检查密钥和模型权限',
    'model_unavailable': '模型服务未找到所选模型或资源，请检查模型名称和地区',
    'rate_limited': '模型服务限流或额度不足，请稍后检查',
    'provider_error': '模型服务拒绝请求或发生错误，请检查该模型和账号状态',
    'invalid_response': '模型服务未返回有效的推理结果，不能确认连通',
    'redirect_rejected': '模型服务返回重定向，本地测试未跟随该地址',
    'oauth_failed': 'Google 服务账号未能取得访问令牌，请检查账号权限和私钥',
}

# Exact identifiers and cross-region support from New API v1.0.0-rc.35,
# relay/channel/aws/constants.go. Unknown names are never synthesized.
_BEDROCK_MODELS = {
    'claude-3-sonnet-20240229': ('anthropic.claude-3-sonnet-20240229-v1:0', ('us', 'eu', 'ap')),
    'claude-3-opus-20240229': ('anthropic.claude-3-opus-20240229-v1:0', ('us',)),
    'claude-3-haiku-20240307': ('anthropic.claude-3-haiku-20240307-v1:0', ('us', 'eu', 'ap')),
    'claude-3-5-sonnet-20240620': ('anthropic.claude-3-5-sonnet-20240620-v1:0', ('us', 'eu', 'ap')),
    'claude-3-5-sonnet-20241022': ('anthropic.claude-3-5-sonnet-20241022-v2:0', ('us', 'ap')),
    'claude-3-5-haiku-20241022': ('anthropic.claude-3-5-haiku-20241022-v1:0', ('us',)),
    'claude-3-7-sonnet-20250219': ('anthropic.claude-3-7-sonnet-20250219-v1:0', ('us', 'eu', 'ap')),
    'claude-sonnet-4-20250514': ('anthropic.claude-sonnet-4-20250514-v1:0', ('us', 'eu', 'ap')),
    'claude-opus-4-20250514': ('anthropic.claude-opus-4-20250514-v1:0', ('us',)),
    'claude-opus-4-1-20250805': ('anthropic.claude-opus-4-1-20250805-v1:0', ('us',)),
    'claude-sonnet-4-5-20250929': ('anthropic.claude-sonnet-4-5-20250929-v1:0', ('us', 'eu', 'ap')),
    'claude-haiku-4-5-20251001': ('anthropic.claude-haiku-4-5-20251001-v1:0', ('us', 'eu', 'ap')),
    'claude-opus-4-5-20251101': ('anthropic.claude-opus-4-5-20251101-v1:0', ('us', 'eu', 'ap')),
    'claude-sonnet-4-6': ('anthropic.claude-sonnet-4-6', ('us', 'eu', 'ap')),
    'claude-opus-4-6': ('anthropic.claude-opus-4-6-v1', ('us', 'eu', 'ap')),
    'claude-opus-4-7': ('anthropic.claude-opus-4-7', ('us', 'eu', 'ap')),
    'claude-opus-4-8': ('anthropic.claude-opus-4-8', ('us', 'eu', 'ap')),
}
_VERTEX_CLAUDE = {
    'claude-3-sonnet-20240229': 'claude-3-sonnet@20240229',
    'claude-3-opus-20240229': 'claude-3-opus@20240229',
    'claude-3-haiku-20240307': 'claude-3-haiku@20240307',
    'claude-3-5-sonnet-20240620': 'claude-3-5-sonnet@20240620',
    'claude-3-5-sonnet-20241022': 'claude-3-5-sonnet-v2@20241022',
    'claude-3-5-haiku-20241022': 'claude-3-5-haiku@20241022',
    'claude-3-7-sonnet-20250219': 'claude-3-7-sonnet@20250219',
    'claude-sonnet-4-20250514': 'claude-sonnet-4@20250514',
    'claude-opus-4-20250514': 'claude-opus-4@20250514',
    'claude-opus-4-1-20250805': 'claude-opus-4-1@20250805',
    'claude-sonnet-4-5-20250929': 'claude-sonnet-4-5@20250929',
    'claude-haiku-4-5-20251001': 'claude-haiku-4-5@20251001',
    'claude-opus-4-5-20251101': 'claude-opus-4-5@20251101',
}


class _ProbeFailure(Exception):
    def __init__(self, code, http_status=None, provider_message=None):
        self.code = code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.provider_message = provider_message
        super().__init__(code)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _object(value):
    if value in (None, ''):
        return {}
    if isinstance(value, str):
        try:
            value = load_credential_json(value)
        except (ValueError, RecursionError):
            raise _ProbeFailure('invalid_configuration') from None
    if not isinstance(value, dict):
        raise _ProbeFailure('invalid_configuration')
    return value


def _text(value, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise _ProbeFailure('invalid_configuration')
    return value


def _test_content(value):
    if value is None:
        return _PROMPT
    if not isinstance(value, str) or len(value) > 1000:
        raise _ProbeFailure('invalid_test_content')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise _ProbeFailure('invalid_test_content') from None
    return value if value.strip() else _PROMPT


_MESSAGE_LIMIT = 1500
_RESPONSE_LIMIT = 65536
_SECRET_FIELDS = frozenset({'key', 'api_key', 'apikey', 'access_token', 'refresh_token', 'id_token',
    'session_token', 'secret_access_key', 'aws_secret_access_key', 'access_key_id', 'aws_access_key_id',
    'private_key', 'private_key_id', 'client_secret', 'password', 'assertion', 'token', 'signature'})
_SECRET_FIELD_NAMES = frozenset(name.replace('_', '') for name in _SECRET_FIELDS)


def _secret_field(name):
    return re.sub(r'[-_]', '', str(name).lower()) in _SECRET_FIELD_NAMES


def _remember_secret(secrets, value):
    if not isinstance(value, str) or not value:
        return
    secrets.add(value)
    if 'PRIVATE KEY-----' in value:
        lines = [line.strip() for line in value.splitlines() if line and not line.startswith('-----')]
        secrets.update(lines)
        secrets.add(''.join(lines))


def _remember_fields(secrets, value, depth=0):
    if not isinstance(value, dict) or depth > 3:
        return
    for name, field in list(value.items())[:40]:
        if _secret_field(name):
            _remember_secret(secrets, field)
        elif isinstance(field, dict):
            _remember_fields(secrets, field, depth + 1)


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'template', 'svg'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'template', 'svg') and self.hidden:
            self.hidden -= 1
        elif not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean_message(value):
    return ' '.join(''.join(c for c in value if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs')
                           or c in '\n\r\t').split())


def _provider_message(value, secrets, *, html=False):
    if not isinstance(value, str) or not value or len(value) > _RESPONSE_LIMIT:
        return None
    if html:
        parser = _ReadableHTML()
        try:
            parser.feed(value)
            parser.close()
            value = ' '.join(parser.parts)
        except (ValueError, AssertionError):
            return None
    value = _clean_message(value)
    variants = set()
    for secret in secrets:
        if not secret:
            continue
        variants.update((secret, _clean_message(secret), quote(secret, safe=''), quote_plus(secret),
                         json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]))
    for secret in sorted(variants - {''}, key=len, reverse=True):
        value = value.replace(secret, '[REDACTED]')
    return (value[:_MESSAGE_LIMIT - 1] + '…' if len(value) > _MESSAGE_LIMIT else value) or None


def _error_message(data):
    if not isinstance(data, dict):
        return data if isinstance(data, str) else None
    error = data.get('error')
    candidates = ([error.get(name) for name in ('message', 'Message', 'detail', 'error_description')]
                  if isinstance(error, dict) else [])
    candidates.extend(data.get(name) for name in ('message', 'Message', 'detail', 'error_description'))
    candidates.append(error)
    if isinstance(error, dict):
        candidates.extend(error.get(name) for name in ('code', 'type'))
    return next((value for value in candidates if isinstance(value, str) and value.strip()), None)


def _request(url, headers, body, before_request, secrets):
    if any(not isinstance(value, str) or any(ord(c) < 32 or ord(c) > 126 for c in value) for value in headers.values()):
        raise _ProbeFailure('invalid_credential')
    for name, value in headers.items():
        if name.lower() in ('authorization', 'api-key', 'x-api-key', 'x-goog-api-key', 'x-amz-security-token'):
            _remember_secret(secrets, value)
            if name.lower() == 'authorization':
                if value.startswith(('Bearer ', 'Basic ')):
                    _remember_secret(secrets, value.split(' ', 1)[1])
                for match in re.finditer(r'(?:Credential|Signature)=([^, /]+)', value):
                    _remember_secret(secrets, match[1])
    for name, value in parse_qsl(urlsplit(url).query):
        if _secret_field(name):
            _remember_secret(secrets, value)
    if headers.get('Content-Type') == 'application/x-www-form-urlencoded':
        for name, value in parse_qsl(body.decode('utf-8')):
            if _secret_field(name):
                _remember_secret(secrets, value)
                if name.lower() == 'assertion':
                    for segment in value.split('.'):
                        _remember_secret(secrets, segment)
    # Includes OAuth: a removed/cancelled/expired job must never issue a request.
    # Keep this outside try so even a guard's ValueError propagates unchanged.
    if before_request is not None:
        before_request()
    try:
        response = safe_request('POST', url, headers=headers, data=body, timeout=35)
    except httpx.TimeoutException:
        raise _ProbeFailure('timeout') from None
    except (httpx.TransportError, OSError, ValueError):
        raise _ProbeFailure('connection_error') from None
    status = response.status_code
    if type(status) is not int or not 100 <= status <= 599:
        raise _ProbeFailure('invalid_response')
    data, text = None, None
    try:
        # safe_request already enforces its response-size limit. Do not reject
        # valid large embedding responses because the display text is bounded.
        data = response.json()
    except (ValueError, RecursionError):
        if len(response.content) <= _RESPONSE_LIMIT:
            text = response.text
    _remember_fields(secrets, data)
    html = text is not None and ('html' in response.headers.get('content-type', '').lower()
        or re.search(r'<!doctype\s+html|</?(?:html|head|body)(?:\s|>)', text, re.IGNORECASE))
    message = _provider_message(_error_message(data) if data is not None else text, secrets, html=bool(html))
    if 300 <= status < 400:
        raise _ProbeFailure('redirect_rejected', status, message)
    if status in (401, 403):
        raise _ProbeFailure('authentication_failed', status, message)
    if status == 404:
        raise _ProbeFailure('model_unavailable', status, message)
    if status == 429:
        raise _ProbeFailure('rate_limited', status, message)
    if not 200 <= status < 300:
        raise _ProbeFailure('provider_error', status, message)
    if not isinstance(data, dict) or data.get('error'):
        raise _ProbeFailure('invalid_response', status, message)
    return data, status


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _google_token(account, before_request, secrets):
    try:
        signing_key = serialization.load_pem_private_key(account['private_key'].encode(), password=None)
        if not isinstance(signing_key, rsa.RSAPrivateKey):
            raise TypeError
        issued = int(time.time())
        claims = {'iss': account['client_email'], 'scope': 'https://www.googleapis.com/auth/cloud-platform',
                  'aud': _TOKEN_URL, 'iat': issued, 'exp': issued + 300}
        unsigned = (_b64(_json_bytes({'alg': 'RS256', 'typ': 'JWT'})) + '.' + _b64(_json_bytes(claims))).encode()
        assertion = unsigned.decode() + '.' + _b64(signing_key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256()))
    except (ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        raise _ProbeFailure('invalid_credential') from None
    # User-provided token_uri is intentionally ignored: never send a JWT elsewhere.
    data, status = _request(_TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                    urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                               'assertion': assertion}).encode(), before_request, secrets)
    token = data.get('access_token')
    if (not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token)
            or len(token) > 16384 or str(data.get('token_type', '')).lower() != 'bearer'):
        raise _ProbeFailure('oauth_failed', status, _provider_message(_error_message(data), secrets))
    return token


def _sigv4(url, body, access_key, secret_key, region, now=None):
    """Sign the exact bytes sent, following AWS SDK's non-S3 URI normalization."""
    stamp = (now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')
    day = stamp[:8]
    host = urlsplit(url).netloc
    digest = hashlib.sha256(body).hexdigest()
    signed_headers = 'content-type;host;x-amz-date'
    canonical_headers = f'content-type:application/json\nhost:{host}\nx-amz-date:{stamp}\n'
    # AWS non-S3 SigV4 double-encodes the already escaped HTTP path.
    path = quote(urlsplit(url).path, safe='/~')
    canonical = f'POST\n{path}\n\n{canonical_headers}\n{signed_headers}\n{digest}'
    scope = f'{day}/{region}/bedrock/aws4_request'
    to_sign = '\n'.join(('AWS4-HMAC-SHA256', stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    signing = ('AWS4' + secret_key).encode()
    for part in (day, region, 'bedrock', 'aws4_request'):
        signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()
    return {'Content-Type': 'application/json', 'X-Amz-Date': stamp,
            'Authorization': f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}'}


def _bedrock_model(model, region, explicit):
    if explicit or model not in _BEDROCK_MODELS:
        return model
    target, areas = _BEDROCK_MODELS[model]
    area = region.split('-')[0]
    if area in areas:
        target = ('apac' if area == 'ap' else area) + '.' + target
    return target


def _region(config, original_model):
    value = config.get('other') or 'global'
    if isinstance(value, dict) or isinstance(value, str) and value.startswith('{'):
        value = _object(value)
        value = value.get(original_model, value.get('default', 'global'))
    if not isinstance(value, str) or re.fullmatch(r'(?:global|[a-z]+(?:-[a-z]+)*\d)', value) is None:
        raise _ProbeFailure('invalid_configuration')
    return value


def _azure_base(config, claude=False):
    base = config.get('base_url') or ''
    suffix = r'\.services\.ai\.azure\.com/anthropic' if claude else r'\.openai\.azure\.com'
    if not isinstance(base, str) or re.fullmatch(r'https://[a-z0-9][a-z0-9-]{0,62}[a-z0-9]' + suffix + '/?', base) is None:
        raise _ProbeFailure('invalid_configuration')
    return base.rstrip('/')


def _anthropic_request(key, model, base, content):
    return (base + '/v1/messages', {'Content-Type': 'application/json', 'x-api-key': key,
                                   'anthropic-version': '2023-06-01'},
            {'model': model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': content}]}, 'anthropic')


def _google_request(key, schema, model, original_model, config, before_request, content, secrets):
    kind = schema['type']
    claude = kind == 'vertex_claude' or model.startswith('claude-')
    publisher = 'anthropic' if claude else 'google'
    model = _VERTEX_CLAUDE.get(model, model) if claude else model.removeprefix('models/')
    region = _region(config, original_model)
    headers = {'Content-Type': 'application/json'}
    if key.startswith('{'):
        account = _object(key)
        project = account.get('project_id')
        if not isinstance(project, str) or re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]|\d{6,20}', project) is None:
            raise _ProbeFailure('invalid_credential')
        host = 'aiplatform.googleapis.com' if region == 'global' else region + '-aiplatform.googleapis.com'
        prefix = f'https://{host}/v1/projects/{project}/locations/{region}'
        headers['Authorization'] = 'Bearer ' + _google_token(account, before_request, secrets)
        headers['x-goog-user-project'] = project
    else:
        # Express-mode API-key paths have neither project nor location segments.
        prefix = 'https://aiplatform.googleapis.com/v1'
        headers['x-goog-api-key'] = key
    url = f'{prefix}/publishers/{publisher}/models/{quote(model, safe="")}'
    if claude:
        return (url + ':rawPredict', headers,
                {'anthropic_version': 'vertex-2023-10-16', 'max_tokens': 8,
                 'messages': [{'role': 'user', 'content': content}]}, 'anthropic')
    return url + ':generateContent', headers, _gemini_body(content), 'gemini'


def _gemini_body(content):
    return {'contents': [{'role': 'user', 'parts': [{'text': content}]}],
            'generationConfig': {'maxOutputTokens': 8, 'candidateCount': 1}}


def _openai_request(key, model, config, content, azure=False):
    headers = {'Content-Type': 'application/json', 'api-key' if azure else 'Authorization': key if azure else 'Bearer ' + key}
    if azure:
        base = _azure_base(config)
        version = config.get('other')
        if not isinstance(version, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:-preview)?', version) is None:
            raise _ProbeFailure('invalid_configuration')
        prefix = base + '/openai/deployments/' + quote(model, safe='')
        query = '?' + urlencode({'api-version': version})
    else:
        base = str(config.get('base_url') or 'https://api.openai.com').rstrip('/')
        if base not in ('https://api.openai.com', 'https://api.openai.com/v1'):
            raise _ProbeFailure('invalid_configuration')
        prefix, query = 'https://api.openai.com/v1', ''
        if config.get('openai_organization'):
            headers['OpenAI-Organization'] = _text(config['openai_organization'])
    if model.startswith('text-embedding-'):
        return prefix + '/embeddings' + query, headers, {'model': model, 'input': content}, 'embedding'
    if not azure and 'moderation' in model:
        return prefix + '/moderations', headers, {'model': model, 'input': content}, 'moderation'
    if any(part in model.lower() for part in ('realtime', 'transcribe', 'whisper', 'tts', 'dall-e', 'gpt-image', 'sora')):
        raise _ProbeFailure('unsupported_model_kind')
    # Modern reasoning/Codex use Responses; do not retry with another API on error.
    if re.match(r'gpt-[5-9](?:[.-]|$)', model) or re.match(r'o[1-9](?:-|$)', model) or 'codex' in model:
        # Azure's current v1 Responses endpoint has no dated api-version query.
        endpoint = base + '/openai/v1/responses' if azure else prefix + '/responses'
        return endpoint, headers, {'model': model, 'input': content, 'max_output_tokens': 16, 'store': False}, 'responses'
    payload = {'model': model, 'messages': [{'role': 'user', 'content': content}]}
    payload['max_completion_tokens' if re.match(r'(?:gpt-[5-9]|o[1-9])', model) else 'max_tokens'] = 8
    return prefix + '/chat/completions' + query, headers, payload, 'chat'


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _text_parts(parts):
    return isinstance(parts, list) and any(isinstance(part, dict) and isinstance(part.get('text'), str) and bool(part['text']) for part in parts)


def _valid_result(data, protocol):
    if protocol == 'anthropic':
        return data.get('type') == 'message' and data.get('role') == 'assistant' and (
            _text_parts(data.get('content')) or data.get('stop_reason') == 'max_tokens' and _positive((data.get('usage') or {}).get('output_tokens')))
    if protocol == 'bedrock':
        message = (data.get('output') or {}).get('message') or {}
        return message.get('role') == 'assistant' and _text_parts(message.get('content'))
    if protocol == 'gemini':
        candidates = data.get('candidates')
        usage = data.get('usageMetadata') or {}
        return isinstance(candidates, list) and any(isinstance(item, dict) and (
            _text_parts((item.get('content') or {}).get('parts'))
            or item.get('finishReason') == 'MAX_TOKENS' and (
                _positive(usage.get('candidatesTokenCount')) or _positive(usage.get('thoughtsTokenCount')))) for item in candidates)
    if protocol == 'chat':
        choices = data.get('choices')
        return isinstance(choices, list) and any(isinstance(item, dict) and isinstance(item.get('message'), dict)
            and item['message'].get('role') == 'assistant' and (
                isinstance(item['message'].get('content'), str) and bool(item['message']['content'])
                or item.get('finish_reason') == 'length' and _positive((data.get('usage') or {}).get('completion_tokens')))
            for item in choices)
    if protocol == 'responses':
        output = data.get('output')
        return data.get('object') == 'response' and isinstance(output, list) and (
            data.get('status') == 'completed' and any(isinstance(item, dict) and item.get('type') == 'message' and _text_parts(item.get('content')) for item in output)
            or data.get('status') == 'incomplete' and (data.get('incomplete_details') or {}).get('reason') == 'max_output_tokens'
            and _positive((data.get('usage') or {}).get('output_tokens')))
    if protocol == 'embedding':
        rows = data.get('data')
        return isinstance(rows, list) and bool(rows) and all(isinstance(row, dict) and isinstance(row.get('embedding'), list)
            and bool(row['embedding']) and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in row['embedding']) for row in rows)
    if protocol == 'moderation':
        rows = data.get('results')
        return isinstance(rows, list) and bool(rows) and all(isinstance(row, dict) and isinstance(row.get('flagged'), bool) and isinstance(row.get('categories'), dict) for row in rows)
    return False


def _assistant_message(data, protocol):
    """Only documented assistant-text fields; never copy tool arguments or tokens."""
    def texts(parts):
        if not isinstance(parts, list):
            return []
        return [part['text'] for part in parts[:20] if isinstance(part, dict)
                and isinstance(part.get('text'), str) and part.get('type', 'text') in ('text', 'output_text')
                and part.get('thought') is not True]

    def rows(value):
        return value[:20] if isinstance(value, list) else []

    def obj(value):
        return value if isinstance(value, dict) else {}

    values = []
    if protocol == 'anthropic':
        values = texts(data.get('content'))
    elif protocol == 'bedrock':
        values = texts(obj(obj(data.get('output')).get('message')).get('content'))
    elif protocol == 'gemini':
        for candidate in rows(data.get('candidates')):
            content = obj(obj(candidate).get('content'))
            if content.get('role') in (None, 'model'):
                values.extend(texts(content.get('parts')))
    elif protocol == 'chat':
        for choice in rows(data.get('choices')):
            message = obj(obj(choice).get('message'))
            if message.get('role') == 'assistant':
                content = message.get('content')
                values.extend([content] if isinstance(content, str) else texts(content))
    elif protocol == 'responses':
        for output in rows(data.get('output')):
            if isinstance(output, dict) and output.get('type') == 'message' and output.get('role') in (None, 'assistant'):
                values.extend(texts(output.get('content')))
    return '\n'.join(values) or None


def probe_credential(key: str, schema: dict, model: str, channel_config: dict, proxy: str = '', before_request=None,
                     *, content: str | None = None) -> dict:
    """Perform at most one inference; OAuth may precede it. Never return secrets.

    ``schema`` describes the saved source credential, including composite Azure
    and business Vertex/AWS kinds. ``channel_config`` supplies frozen mapping,
    region and required api.aws tenant endpoint. Optional ``content`` is limited
    to 1000 characters. Only sanitized provider text is returned; response
    objects, authorization material and OAuth tokens never leave this module.
    Guard exceptions propagate.
    """
    started = time.monotonic()
    secrets = set()
    _remember_secret(secrets, key)
    try:
        content = _test_content(content)
        if proxy:
            raise _ProbeFailure('proxy_unsupported')
        config = _object(channel_config)
        if config.get('proxy') or _object(config.get('setting')).get('proxy'):
            raise _ProbeFailure('proxy_unsupported')
        if not isinstance(schema, dict) or not isinstance(key, str):
            raise _ProbeFailure('invalid_credential')
        if key.startswith('{'):
            _remember_fields(secrets, _object(key))
        original_model = _text(model, 1024)
        mapping = _object(config.get('model_mapping'))
        explicit = original_model in mapping
        model = _text(mapping.get(original_model, original_model), 1024)
        try:
            key = normalize_credential(key, SimpleNamespace(schema_config=schema))
            wire = credential_wire_schema(key, schema)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise _ProbeFailure('invalid_credential') from None
        _remember_secret(secrets, key)
        if key.startswith('{'):
            _remember_fields(secrets, _object(key))
        kind, remote_type = schema.get('type'), schema.get('remote_type')
        if kind in ('azure_gpt', 'azure_claude'):
            fields = azure_credential_fields(key, kind)
            key = fields['key']
            _remember_secret(secrets, key)
            config = {**config, **fields['channel_config']}
        if remote_type == 33 and wire['type'] in ('aws_ak_sk', 'aws_api_key'):
            parts = key.split('|')
            for credential in parts[:-1]:
                _remember_secret(secrets, credential)
            region = parts[-1]
            target = _bedrock_model(model, region, explicit)
            suffix = 'amazonaws.com.cn' if region.startswith('cn-') else 'amazonaws.com'
            url = f'https://bedrock-runtime.{region}.{suffix}/model/{quote(target, safe="")}/converse'
            body = _json_bytes({'messages': [{'role': 'user', 'content': [{'text': content}]}], 'inferenceConfig': {'maxTokens': 8}})
            headers = _sigv4(url, body, parts[0], parts[1], region) if len(parts) == 3 else {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + parts[0]}
            data, http_status = _request(url, headers, body, before_request, secrets)
            protocol = 'bedrock'
        else:
            if remote_type == 41 and kind in ('vertex_json', 'vertex_api_key', 'vertex_gemini', 'vertex_claude'):
                request = _google_request(key, schema, model, original_model, config, before_request, content, secrets)
            elif remote_type == 24:
                request = ('https://generativelanguage.googleapis.com/v1beta/models/' + quote(model.removeprefix('models/'), safe='') + ':generateContent',
                           {'Content-Type': 'application/json', 'x-goog-api-key': key}, _gemini_body(content), 'gemini')
            elif remote_type in (1, 3):
                request = _openai_request(key, model, config, content, azure=remote_type == 3)
            elif remote_type == 20:
                request = ('https://openrouter.ai/api/v1/chat/completions',
                           {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
                           {'model': model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': content}]}, 'chat')
            elif remote_type == 14:
                base = _text(config.get('base_url') or 'https://api.anthropic.com').rstrip('/')
                if kind == 'aws_claude':
                    try:
                        base = validate_aws_claude_base_url(base)
                    except ValueError:
                        raise _ProbeFailure('invalid_configuration') from None
                elif kind == 'azure_claude' or '.services.ai.azure.com/' in base:
                    base = _azure_base(config, claude=True)
                elif base not in ('https://api.anthropic.com', 'https://opencode.ai/zen'):
                    raise _ProbeFailure('invalid_configuration')
                request = _anthropic_request(key, model, base, content)
            else:
                raise _ProbeFailure('unsupported_protocol')
            url, headers, payload, protocol = request
            data, http_status = _request(url, headers, _json_bytes(payload), before_request, secrets)
        try:
            valid = _valid_result(data, protocol)
        except (AttributeError, TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise _ProbeFailure('invalid_response', http_status, _provider_message(_error_message(data), secrets))
        provider_message = _provider_message(_assistant_message(data, protocol), secrets)
    except _ProbeFailure as exc:
        return {'success': False, 'message': ERROR_MESSAGES[exc.code], 'error_code': exc.code,
                'http_status': exc.http_status,
                'provider_message': _provider_message(exc.provider_message, secrets),
                'latency_ms': max(0, round((time.monotonic() - started) * 1000))}
    return {'success': True, 'message': '本地后台已使用保存的凭据完成模型调用',
            'http_status': http_status,
            'provider_message': provider_message,
            'latency_ms': max(0, round((time.monotonic() - started) * 1000))}
