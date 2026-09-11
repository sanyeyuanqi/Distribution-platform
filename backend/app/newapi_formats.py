"""Versioned, fixed New API credential wire formats (v0.13.2 / v1.0.0-rc.35).

The catalog describes channel management, not whether an upstream account can run
a particular model. Credentials are never inferred from arbitrary user schemas.
"""
import base64
import binascii
import json
import re
from copy import deepcopy
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from .google_services import CLAUDE9
from .security import fingerprint, mask

BEDROCK_DEFAULT_REGION = 'us-east-1'
BEDROCK_INPUT_CONTRACT = 'optional-region-v2'
AWS_REGION_PATTERN = r'[a-z]{2}(?:-[a-z]+)+-\d+'
AWS_ACCESS_KEY_PATTERN = r'(?:AKIA|ASIA)[A-Z0-9]{16}'

# Public provider identifiers, independently transcribed from New API's channel catalog.
CHANNEL_TYPES = {
    1: 'OpenAI', 2: 'Midjourney', 3: 'Azure', 4: 'Ollama', 5: 'MidjourneyPlus',
    6: 'OpenAIMax', 7: 'OhMyGPT', 8: 'Custom', 9: 'AILS', 10: 'AIProxy', 11: 'PaLM',
    12: 'API2GPT', 13: 'AIGC2D', 14: 'Anthropic', 15: 'Baidu', 16: 'Zhipu',
    17: 'Ali', 18: 'Xunfei', 19: '360', 20: 'OpenRouter', 21: 'AIProxyLibrary',
    22: 'FastGPT', 23: 'Tencent', 24: 'Google', 25: 'Moonshot', 26: 'ZhipuV4',
    27: 'Perplexity', 31: 'LingYiWanWu', 33: 'AWS', 34: 'Cohere', 35: 'MiniMax',
    36: 'SunoAPI', 37: 'Dify', 38: 'Jina', 39: 'Cloudflare', 40: 'SiliconFlow',
    41: 'VertexAI', 42: 'Mistral', 43: 'DeepSeek', 44: 'MokaAI', 45: 'VolcEngine',
    46: 'BaiduV2', 47: 'Xinference', 48: 'xAI', 49: 'Coze', 50: 'Kling',
    51: 'Jimeng', 52: 'Vidu', 53: 'Submodel', 54: 'DoubaoVideo', 55: 'Sora',
    56: 'Replicate', 57: 'Codex', 59: 'Sub2API', 60: 'NewAPI',
}
_SPECIAL = {
    15: [('baidu_pair', 'API Key / Secret Key', 'APIKey|SecretKey')],
    16: [('zhipu_pair', 'ID / Secret', 'ID.Secret')],
    18: [('xunfei_triple', '讯飞身份凭据', 'APPID|APISecret|APIKey')],
    23: [('tencent_triple', '腾讯云身份凭据', 'AppId|SecretId|SecretKey')],
    33: [('aws_ak_sk', 'AWS IAM（AK / SK）', 'AccessKeyID|SecretAccessKey|Region'),
         ('aws_api_key', 'AWS API 密钥', 'APIKey|Region')],
    41: [('vertex_json', 'Vertex AI 服务账号 JSON', '{"project_id":"…","client_email":"…","private_key":"…"}'),
         ('vertex_api_key', 'Vertex AI API 密钥', '输入 Vertex AI API Key')],
    50: [('kling_pair', 'Kling AK / SK', 'AccessKey|SecretKey')],
    51: [('jimeng_pair', '即梦 AK / SK', 'AccessKey|SecretKey')],
    57: [('codex_json', 'Codex 账号 JSON', '{"access_token":"…","account_id":"…"}')],
}
JSON_KINDS = frozenset({'vertex_json', 'codex_json'})
_SPECS = []
for _number, _family in CHANNEL_TYPES.items():
    for _kind, _name, _placeholder in _SPECIAL.get(_number, [('api_key', 'API 密钥', '输入 API Key')]):
        if _number == 24:
            _name = 'AI Studio · Gemini'
        _code = 'api_key-v1' if _number == 1 else f'newapi-{_number}-{_kind.replace("_", "-")}-v1'
        _SPECS.append({
            'family': _family, 'code': _code, 'version': '1', 'name': _name,
            'schema_config': {'type': _kind, 'remote_type': _number},
            'help': (f'NewAPI 固定格式：{_placeholder}。可粘贴单个 JSON 对象；批量使用对象数组或每行一个 JSON。'
                     if _kind in JSON_KINDS else f'NewAPI 固定格式：{_placeholder}。批量时每行一条完整凭据。'),
            'placeholder': _placeholder, 'json': _kind in JSON_KINDS,
            'remote_type': _number,
        })

# Business input formats can share a NewAPI wire protocol while keeping their
# own validation. Legacy AWS formats remain immutable for existing channels.
_SPECS.extend([
    {'family': 'AWS', 'code': 'newapi-33-aws-bedrock-v1', 'version': '1', 'name': 'Bedrock 密钥',
     'schema_config': {'type': 'aws_bedrock', 'remote_type': 33},
     'help': '每行填写 AccessKeyID|SecretAccessKey 或一个 Bedrock API Key，两种格式可混合；也可在末尾添加 |Region 指定地区，无需 Base URL。',
     'placeholder': 'AKIAIOSFODNN7EXAMPLE|SecretAccessKey\nBedrockAPIKey\n也可填写 AK|SK|Region 或 APIKey|Region，省略地区时后台自动补全',
     'json': False, 'remote_type': 33},
    {'family': 'AWS', 'code': 'newapi-14-aws-claude-v1', 'version': '1', 'name': 'Claude 代理 (api.aws)',
     'schema_config': {'type': 'aws_claude', 'remote_type': 14},
     'help': 'AWS Claude 新版：填写 API Key 与 Base URL；地址须为真实的 https://<租户>.api.aws，勿填占位示例。本批密钥共用下方地址，每行一个 API Key。',
     'placeholder': '输入 Claude 代理 API Key，每行一个', 'json': False, 'remote_type': 14},
    {'family': 'Azure', 'code': 'newapi-3-azure-gpt-v1', 'version': '1', 'name': 'Azure OpenAI · GPT',
     'schema_config': {'type': 'azure_gpt', 'remote_type': 3},
     'help': '每行填写 Resource|ApiKey|ApiVersion。Resource 为 Azure 资源名；自动生成 https://{resource}.openai.azure.com。API 版本填写日期，例如 2025-04-01-preview。',
     'placeholder': 'resource-a|key1|2025-04-01-preview\nresource-b|key2|2025-04-01-preview', 'json': False, 'remote_type': 3},
    {'family': 'Azure', 'code': 'newapi-14-azure-claude-v1', 'version': '1', 'name': 'Azure · Claude',
     'schema_config': {'type': 'azure_claude', 'remote_type': 14},
     'help': '走 Azure 上的 Claude 模型。每行填写 Resource|ApiKey|ApiVersion，自动生成 https://{resource}.services.ai.azure.com/anthropic。兼容原两段格式；Claude 接口不使用 Azure API 版本，第三段仅随凭据保存。',
     'placeholder': 'resource-a|key1|2025-04-01-preview\nresource-b|key2|2025-04-01-preview', 'json': False, 'remote_type': 14},
    {'family': 'VertexAI', 'code': 'newapi-41-vertex-gemini-v1', 'version': '1', 'name': 'Vertex AI · Gemini',
     'schema_config': {'type': 'vertex_gemini', 'remote_type': 41},
     'help': '走 Gemini 模型。粘贴 GCP 服务账号 JSON（含 project_id、private_key、client_email），支持多行/美化 JSON，多个对象可连续粘贴，系统自动压行。也支持纯 Vertex API Key（一行一个），无需 Base URL。',
     'placeholder': '{"project_id":"项目编号","client_email":"服务账号邮箱","private_key":"私钥"}\nVertexAIAPIKey',
     'json': False, 'input_mode': 'auto', 'remote_type': 41},
    {'family': 'VertexAI', 'code': 'newapi-41-vertex-claude-v1', 'version': '1', 'name': 'Vertex AI · Claude',
     'schema_config': {'type': 'vertex_claude', 'remote_type': 41},
     'help': '走 Vertex 上的 Claude 模型。粘贴 GCP 服务账号 JSON（含 project_id、private_key、client_email），支持多行/美化 JSON 和连续粘贴多个对象。也支持纯 Vertex API Key（一行一个），无需 Base URL；API Key 模式需目标站点版本支持。',
     'placeholder': '{"project_id":"项目编号","client_email":"服务账号邮箱","private_key":"私钥"}\nVertexAIAPIKey',
     'json': False, 'input_mode': 'auto', 'remote_type': 41},
])


def validate_aws_claude_base_url(value):
    """Accept only explicit HTTPS api.aws proxy origins, never example hosts."""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        labels = host.split('.')
        if host.startswith(('bedrock-mantle.', 'bedrock-runtime.', 'aws-external-anthropic.')):
            raise ValueError('specialized_aws_service')
        if (not value or len(value) > 1000 or any(c.isspace() or ord(c) < 32 for c in value)
                or parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or ':' in parsed.netloc or parsed.port is not None or parsed.path not in ('', '/')
                or '?' in value or '#' in value
                or not host.endswith('.api.aws') or len(labels) < 3 or len(host) > 253
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
                or any(label in {'xxx', 'example', 'test', 'tenant', 'your-tenant', 'localhost', 'placeholder'} for label in labels[:-2])):
            raise ValueError
    except (ValueError, AttributeError) as exc:
        if str(exc) == 'specialized_aws_service':
            raise ValueError('此 AWS 官方服务需要专用路径或工作区配置，请填写 Anthropic 兼容租户代理地址') from None
        raise ValueError('请填写真实的 https://<租户>.api.aws 地址，不含端口、路径、账号或查询参数') from None
    return 'https://' + host


def credential_wire_schema(value, schema):
    """Freeze the explicit NewAPI authentication mode for one credential."""
    if (schema or {}).get('type') in ('vertex_gemini', 'vertex_claude'):
        normalized = normalize_vertex_credential(value, schema['type'])
        return {'type': 'vertex_json' if normalized.startswith('{') else 'vertex_api_key', 'remote_type': 41}
    if (schema or {}).get('type') != 'aws_bedrock':
        return deepcopy(schema)
    parts = value.split('|')
    if len(parts) not in (2, 3) or not all(parts) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', parts[-1]):
        raise ValueError('密钥格式应为 AccessKeyID|SecretAccessKey|Region 或 APIKey|Region')
    return {'type': 'aws_api_key' if len(parts) == 2 else 'aws_ak_sk', 'remote_type': 33}


def normalize_bedrock_credential(value):
    """Expand convenient input to the existing, region-explicit NewAPI wire form."""
    parts = value.split('|')
    if not all(parts):
        raise ValueError('Bedrock 密钥不能有空字段；地区可整段省略，但不要保留末尾分隔符')
    if len(parts) == 1:
        if re.fullmatch(AWS_ACCESS_KEY_PATTERN, value):
            raise ValueError('AWS 访问密钥还需要 SecretAccessKey，请填写 AccessKeyID|SecretAccessKey')
        value += '|' + (bedrock_token_region(value) or BEDROCK_DEFAULT_REGION)
    elif len(parts) == 2 and re.fullmatch(AWS_ACCESS_KEY_PATTERN, parts[0]):
        if parts[0].startswith('ASIA'):
            raise ValueError('ASIA 临时访问密钥还需要会话令牌，请改用 AWS 生成的短期 Bedrock API Key')
        if re.fullmatch(AWS_REGION_PATTERN, parts[1]):
            raise ValueError('AWS 访问密钥缺少 SecretAccessKey，请填写 AccessKeyID|SecretAccessKey|Region')
        value += '|' + BEDROCK_DEFAULT_REGION
    credential_wire_schema(value, {'type': 'aws_bedrock', 'remote_type': 33})
    normalized = value.split('|')
    if len(normalized) == 2:
        embedded_region = bedrock_token_region(normalized[0])
        if embedded_region and embedded_region != normalized[1]:
            raise ValueError('填写的 Region 与临时 Bedrock API Key 的签名地区不一致')
    if len(value) > 4096:
        raise ValueError('补全地区后的 Bedrock 凭据不能超过 4096 个字符')
    return value


def bedrock_token_region(value):
    """Read AWS's public short-term token envelope, without validating its signature.

    Source: aws/aws-bedrock-token-generator-python, token_generator._generate_token.
    Long-term ABSK keys and IAM access keys do not expose a documented region.
    No decoded URL is ever requested or returned to the client.
    """
    prefix = 'bedrock-api-key-'
    if not value.startswith(prefix):
        return None
    try:
        encoded = value[len(prefix):]
        if len(value) > 4096:
            raise ValueError
        decoded = base64.b64decode(encoded, validate=True)
        if not decoded or len(decoded) > 3072 or base64.b64encode(decoded).decode('ascii') != encoded:
            raise ValueError
        url = decoded.decode('ascii')
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or re.search(r'%(?![a-fA-F0-9]{2})', url):
            raise ValueError
        parsed = urlsplit('https://' + url)
        if parsed.netloc != 'bedrock.amazonaws.com' or parsed.path != '/' or parsed.fragment:
            raise ValueError
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        query = dict(pairs)
        if len(query) != len(pairs) or any(not v for v in query.values()):
            raise ValueError
        if any(query.get(k) != v for k, v in {
            'Action': 'CallWithBearerToken', 'Version': '1',
            'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-SignedHeaders': 'host',
        }.items()):
            raise ValueError
        scope = query.get('X-Amz-Credential', '').split('/')
        if (len(scope) != 5 or not re.fullmatch(AWS_ACCESS_KEY_PATTERN, scope[0])
                or not re.fullmatch(r'\d{8}', scope[1]) or not re.fullmatch(AWS_REGION_PATTERN, scope[2])
                or scope[3:] != ['bedrock', 'aws4_request']):
            raise ValueError
        timestamp = query.get('X-Amz-Date', '')
        if not re.fullmatch(r'\d{8}T\d{6}Z', timestamp) or timestamp[:8] != scope[1]:
            raise ValueError
        datetime.strptime(timestamp, '%Y%m%dT%H%M%S%z')
        expires = query.get('X-Amz-Expires', '')
        if not re.fullmatch(r'\d{1,5}', expires) or not 1 <= int(expires) <= 43200:
            raise ValueError
        if not re.fullmatch(r'[a-fA-F0-9]{64}', query.get('X-Amz-Signature', '')):
            raise ValueError
        return scope[2]
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError('临时 Bedrock API Key 格式无效，请粘贴完整的官方生成密钥') from None


def get_format_specs():
    return deepcopy(_SPECS)


def format_default_models(schema):
    """Selector suggestions only; never replace configured or effective models."""
    return list(CLAUDE9) if schema.get('type') in ('aws_claude', 'vertex_claude') else []


def protocol_schema(schema):
    """Validate configurable definitions against implemented credential parsers."""
    if (not isinstance(schema, dict) or set(schema) - {'type', 'remote_type', 'help', 'placeholder'}
            or not isinstance(schema.get('type'), str) or type(schema.get('remote_type')) is not int):
        raise ValueError('密钥定义必须包含 type、remote_type，且只能附加 help、placeholder')
    wire = {key: schema[key] for key in ('type', 'remote_type')}
    if not any(spec['schema_config'] == wire for spec in _SPECS):
        raise ValueError('此密钥解析方式与渠道类型组合尚未实现')
    for name, limit in (('help', 4000), ('placeholder', 4000)):
        if name in schema and (not isinstance(schema[name], str) or len(schema[name]) > limit):
            raise ValueError(f'{name} 必须是最多 {limit} 字符的文本')
    return wire


def format_spec(fmt):
    if fmt is None:
        return None
    try:
        wire = protocol_schema(fmt.schema_config)
    except ValueError:
        return None
    base = next(spec for spec in _SPECS if spec['schema_config'] == wire)
    return {**base, 'wire_code': base['code'],
            **{key: getattr(fmt, key, base[key]) for key in ('code', 'name', 'version')},
            **{key: fmt.schema_config[key] for key in ('help', 'placeholder') if key in fmt.schema_config}}


def format_descriptor(fmt, category=None):
    spec = format_spec(fmt)
    if category is not None:
        from .catalog_policy import catalog_format_spec
        spec = catalog_format_spec(category, fmt) or spec
    if not spec:
        return {'code': getattr(fmt, 'code', ''), 'name': getattr(fmt, 'name', ''),
                'help': '此密钥格式尚未实现', 'placeholder': '', 'json': False, 'input_mode': 'text'}
    return {**{key: spec[key] for key in ('code', 'name', 'help', 'placeholder', 'json', 'remote_type')},
            'input_mode': spec.get('input_mode', 'json' if spec['json'] else 'text')}


def implemented_format(category, fmt):
    from .catalog_policy import catalog_supports_protocol
    spec = format_spec(fmt)
    return bool(spec and category and (category.family == spec['family']
                                     or catalog_supports_protocol(category.family, spec['family'])))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON 对象不能包含重复字段')
        result[key] = value
    return result


def load_credential_json(value):
    try:
        result = json.loads(value, object_pairs_hook=_unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON 不允许非有限数字')))
        pending = [(result, 0)]
        while pending:
            node, depth = pending.pop()
            if depth > 64:
                raise ValueError('JSON 嵌套过深')
            if isinstance(node, (dict, list)):
                pending.extend((child, depth + 1) for child in (node.values() if isinstance(node, dict) else node))
        return result
    except (ValueError, RecursionError):
        raise ValueError('密钥必须是合法 JSON，不能包含重复字段或非有限数字') from None


def normalize_vertex_credential(value, kind):
    """Service choice and authentication choice are independent.

    The target adapter must separately establish support for Claude API-key
    requests; accepting the input does not upgrade an older remote protocol.
    """
    from types import SimpleNamespace
    value = value.strip()
    json_input = value.startswith(('{', '['))
    if not json_input and any(char in value for char in ('"', "'", '{', '}', '[', ']')):
        raise ValueError('Vertex AI 凭据格式无效，请填写服务账号 JSON 或完整 API Key')
    schema = {'type': 'vertex_json' if json_input else 'vertex_api_key', 'remote_type': 41}
    normalized = normalize_credential(value, SimpleNamespace(schema_config=schema))
    if json_input:
        obj = load_credential_json(normalized)
        if obj.get('type', 'service_account') != 'service_account':
            raise ValueError('Vertex AI 需要 Google Cloud 服务账号 JSON')
    return normalized


def normalize_credential(value, fmt=None):
    value = value.strip()
    spec = format_spec(fmt) if fmt is not None else None
    if fmt is not None and spec is None:
        raise ValueError('凭据格式未实现或配置已发生变化')
    kind = spec['schema_config']['type'] if spec else 'api_key'
    if kind in ('vertex_gemini', 'vertex_claude'):
        return normalize_vertex_credential(value, kind)
    if kind in JSON_KINDS:
        if not value or len(value) > 65536:
            raise ValueError('JSON 密钥不能为空或超过 65536 个字符')
        obj = load_credential_json(value)
        required = ('project_id', 'client_email', 'private_key') if kind == 'vertex_json' else ('access_token', 'account_id')
        if not isinstance(obj, dict) or any(not isinstance(obj.get(k), str) or not obj[k].strip() for k in required):
            raise ValueError('JSON 密钥需要非空文本字段：' + '、'.join(required))
        # Canonical JSON makes equivalent pasted objects deduplicate consistently.
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if kind in ('azure_gpt', 'azure_claude'):
        from .azure_credentials import normalize_azure_credential
        return normalize_azure_credential(value, kind)
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('API Key 不能为空、包含空白字符或超过 4096 个字符')
    if kind == 'aws_bedrock':
        value = normalize_bedrock_credential(value)
    if kind == 'aws_claude' and any(char in value for char in ('|', ',')):
        raise ValueError('Claude 代理仅填写 API Key；API 地址请在单独的地址栏填写')
    counts = {'aws_ak_sk': 3, 'aws_api_key': 2, 'baidu_pair': 2, 'xunfei_triple': 3,
              'tencent_triple': 3, 'kling_pair': 2, 'jimeng_pair': 2, 'zhipu_pair': 2}
    if kind in counts:
        parts = value.split('.' if kind == 'zhipu_pair' else '|')
        if len(parts) != counts[kind] or not all(parts):
            raise ValueError('密钥格式应为：' + spec['placeholder'])
        if kind.startswith('aws_') and not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', parts[-1]):
            raise ValueError('AWS Region 格式无效，例如 us-east-1')
    return value


def credential_hint(value, fmt=None):
    spec = format_spec(fmt)
    if spec and (spec['json'] or spec['schema_config']['type'] in ('vertex_gemini', 'vertex_claude') and value.startswith('{')):
        return 'JSON ••••' + fingerprint(value)[-6:]
    return mask(value)


def credential_fingerprint(value, fmt=None):
    kind = (fmt.schema_config or {}).get('type') if fmt else None
    if kind in ('vertex_gemini', 'vertex_claude'):
        return fingerprint(json.dumps(['google-service-v1', kind, value], ensure_ascii=False, separators=(',', ':')))
    if kind == 'azure_claude':
        # Azure's optional API-version input does not affect the Claude key or
        # endpoint. Preserve the old two-segment identity for both paste forms.
        return fingerprint('|'.join(value.split('|')[:2]))
    return fingerprint(value)


def validate_vertex_authentication(remote, wire_schema):
    value = remote.get('settings')
    if isinstance(value, str):
        value = load_credential_json(value) if value else {}
    if value is None:
        value = {}
    if not isinstance(value, dict) or value.get('vertex_key_type') not in (None, '', 'json', 'api_key'):
        raise ValueError('远端 Vertex AI 认证设置结构无法核实')
    actual = 'vertex_api_key' if value.get('vertex_key_type') == 'api_key' else 'vertex_json'
    if actual != (wire_schema or {}).get('type'):
        raise ValueError('远端 Vertex AI 认证方式已改变，请同步后核对，不能直接轮换密钥')


def credential_records(credentials, fmt=None):
    """Return logical records and original line numbers; bind metadata after this step."""
    spec = format_spec(fmt)
    text = credentials.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    automatic = spec and spec.get('input_mode') == 'auto'
    if spec and (spec['json'] or automatic) and text.strip():
        try:
            obj = load_credential_json(text)
        except ValueError:
            try:
                return _credential_json_stream(text, automatic=bool(automatic))
            except (ValueError, RecursionError):
                pass
            # JSONL is bound before validation; malformed interior rows are not discarded.
            return lines[start:end], list(range(start + 1, end + 1))
        if isinstance(obj, list):
            return [item if automatic and isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    for item in obj], list(range(1, len(obj) + 1))
        return [text.strip()], [start + 1]
    return lines[start:end], list(range(start + 1, end + 1))


def _credential_json_stream(text, *, automatic):
    """Split pasted JSON objects without splitting a private key's JSON string.

    Objects may be pretty printed, adjacent, or separated by blank lines. In
    auto mode, a plain API key occupies its own line. On any malformed object
    the caller retains the original rows for explicit validation errors.
    """
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    records, line_numbers = [], []
    position, found_json = 0, False
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        start = position
        source_line = text.count('\n', 0, start) + 1
        if text[position] in '{[':
            _, end = decoder.raw_decode(text, position)
            obj = load_credential_json(text[position:end])
            items = obj if isinstance(obj, list) else [obj]
            records.extend(item if automatic and isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                           for item in items)
            line_numbers.extend([source_line] * len(items))
            found_json = True
            position = end
            # An API key cannot trail an object on the same line. Adjacent
            # JSON objects are unambiguous and accepted, even without a gap.
            following = position
            while following < len(text) and text[following].isspace():
                following += 1
            if (following < len(text) and text[following] not in '{['
                    and '\n' not in text[position:following]):
                raise ValueError('JSON 后的 API Key 必须另起一行')
        else:
            if not automatic:
                raise ValueError('JSON 凭据格式无效')
            end = text.find('\n', position)
            if end < 0:
                end = len(text)
            records.append(text[position:end].strip())
            line_numbers.append(source_line)
            position = end
    if not found_json:
        raise ValueError('没有 JSON 记录，按原始行解析')
    return records, line_numbers
