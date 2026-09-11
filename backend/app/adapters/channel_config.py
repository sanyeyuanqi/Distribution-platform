"""Fixed, provider-aware channel payloads; never forward arbitrary JSON settings."""
import json
from types import SimpleNamespace

from ..newapi_formats import (
    credential_wire_schema,
    get_format_specs,
    normalize_bedrock_credential,
    normalize_credential,
    protocol_schema,
    validate_aws_claude_base_url,
)
from ..template_settings import ChannelConfig, validate_proxy
from .silicon import RemoteError


def json_object(value):
    if value in (None, ''):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TypeError('Expected object')
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def checked_config(config, channel_type, supported_types):
    if type(channel_type) is not int or channel_type not in supported_types:
        raise RemoteError('该平台版本尚未支持此渠道类型', category='configuration_error')
    if config is not None and not isinstance(config, dict):
        raise RemoteError('渠道模板配置必须是对象', category='configuration_error')
    raw = dict(config or {})
    schema = raw.pop('credential_format', None)
    candidates = [s for s in get_format_specs() if s['remote_type'] == channel_type]
    if schema is None and len(candidates) == 1:
        schema = candidates[0]['schema_config']
    elif schema is None and channel_type in (3, 14):
        schema = {'type': 'api_key', 'remote_type': channel_type}
    try:
        schema = protocol_schema(schema)
    except ValueError:
        raise RemoteError('渠道类型与密钥解析方式不匹配', category='configuration_error') from None
    spec = next((s for s in candidates if s['schema_config'] == schema), None)
    if spec is None:
        raise RemoteError('渠道类型与固定密钥格式不匹配', category='configuration_error')
    try:
        settings = ChannelConfig.model_validate(raw).model_dump(mode='json')
    except ValueError:
        # Pydantic errors may include an input value: do not expose them here.
        raise RemoteError('渠道模板配置无效，请检查字段和取值', category='configuration_error') from None
    return settings, spec


def config_fields(config, channel_type, *, supported_types, proxy='', routing=True,
                  supports_proxy=False, supports_rpm=False, object_settings=False,
                  bedrock_api_key_sdk=False):
    cfg, spec = checked_config(config, channel_type, supported_types)
    try:
        proxy = validate_proxy(proxy or '')
    except (ValueError, AttributeError):
        raise RemoteError('代理配置格式无效', category='configuration_error') from None
    if proxy and not supports_proxy:
        raise RemoteError('此平台尚未支持代理配置', category='configuration_error')
    if cfg['rpm_enabled'] and not supports_rpm:
        raise RemoteError('此平台不支持远端 RPM 保护', category='configuration_error')
    if not routing and (cfg['priority'] != 0 or cfg['weight'] != 1):
        raise RemoteError('供应商账号没有调整优先级和权重的权限', category='permission_denied')
    if channel_type in (3, 8, 59, 60) and not cfg['base_url']:
        raise RemoteError('此渠道类型必须配置接口地址', category='configuration_error')
    if channel_type in (18, 39, 49) and not cfg['other'].strip():
        raise RemoteError('此渠道类型必须配置额外参数', category='configuration_error')
    if channel_type == 41:
        try:
            regions = json_object(cfg['other'])
            if not regions.get('default') or any(not isinstance(v, str) or not v.strip() for v in regions.values()):
                raise ValueError
        except (ValueError, TypeError):
            raise RemoteError('Vertex AI 部署地区必须为包含 default 的 JSON 对象', category='configuration_error') from None
    if cfg['azure_responses_version'] and channel_type != 3:
        raise RemoteError('Azure Responses 版本仅适用于 Azure 渠道', category='configuration_error')
    extra = {}
    kind = spec['schema_config']['type']
    if kind == 'aws_claude':
        try:
            cfg['base_url'] = validate_aws_claude_base_url(cfg['base_url'])
        except ValueError as exc:
            raise RemoteError(str(exc), category='configuration_error') from None
    if channel_type == 33:
        # Reviewed official releases use the SDK's two-part Bearer parser to
        # avoid their broken explicit API-key transport. The local credential
        # kind and partition remain API Key; forks retain their own contract.
        extra['aws_key_type'] = ('api_key' if kind == 'aws_api_key' and not bedrock_api_key_sdk
                                 else 'ak_sk')
    if channel_type == 41:
        extra['vertex_key_type'] = 'api_key' if kind == 'vertex_api_key' else 'json'
    if cfg['azure_responses_version']:
        extra['azure_responses_version'] = cfg['azure_responses_version']
    setting = {}
    if proxy:
        setting['proxy'] = proxy
    if supports_rpm and cfg['rpm_enabled']:
        setting.update(rate_limit_enabled=True, rpm_limit=cfg['rpm_limit'])
    fields = {
        'type': channel_type, 'base_url': cfg['base_url'] or None,
        'openai_organization': cfg['organization'] or None, 'other': cfg['other'],
        'status': cfg['status'], 'auto_ban': cfg['auto_ban'],
        'model_mapping': encode(cfg['model_mapping']) if cfg['model_mapping'] else None,
        'status_code_mapping': encode(cfg['status_code_mapping']) if cfg['status_code_mapping'] else None,
        'setting': encode(setting), 'settings': extra if object_settings else encode(extra),
    }
    # Existing untemplated uploads keep the platform's routing defaults.
    if config and routing:
        fields.update(priority=cfg['priority'], weight=cfg['weight'])
    return fields, spec


def create_payload(*, name, key, models, remark, group, channel_type, config, keys=None, **options):
    if keys is not None:
        if not isinstance(keys, list) or len(keys) < 2:
            raise RemoteError('多密钥容器至少需要两个密钥', category='configuration_error')
        payloads = [create_payload(name=name, key=entry, models=models, remark=remark, group=group,
                    channel_type=channel_type, config=config, **options) for entry in keys]
        if any({k: v for k, v in payload.items() if k != 'key'} !=
               {k: v for k, v in payloads[0].items() if k != 'key'} for payload in payloads[1:]):
            raise RemoteError('多密钥容器不能混用认证或资源配置', category='configuration_error')
        payload = payloads[0]
        schema = (config or {}).get('credential_format', {})
        normalized = [row['key'] for row in payloads]
        if schema.get('type') in ('aws_bedrock', 'vertex_gemini', 'vertex_claude'):
            wire_schemas = [credential_wire_schema(entry, schema) for entry in normalized]
            if any(wire != wire_schemas[0] for wire in wire_schemas[1:]):
                raise RemoteError('多密钥容器不能混用认证方式', category='configuration_error')
            schema = wire_schemas[0]
        if len(set(normalized)) != len(normalized):
            raise RemoteError('多密钥容器包含重复密钥', category='configuration_error')
        payload['key'] = wire_key_text(normalized, schema)
        return payload
    from ..google_services import service_model_issues
    if issues := service_model_issues((config or {}).get('credential_format', {'remote_type': channel_type}), models):
        raise RemoteError('；'.join(issues), category='configuration_error')
    if (config or {}).get('credential_format', {}).get('type') in ('azure_gpt', 'azure_claude'):
        from ..azure_credentials import azure_credential_fields
        try:
            fields = azure_credential_fields(key, config['credential_format']['type'])
        except ValueError:
            raise RemoteError('Azure 凭据格式无效', category='configuration_error') from None
        key = fields['key']
        config = {**config, **fields['channel_config'], 'credential_format': fields['credential_format']}
    if (config or {}).get('credential_format', {}).get('type') == 'aws_bedrock':
        try:
            key = normalize_bedrock_credential(key.strip())
            config = {**config, 'credential_format': credential_wire_schema(key, config['credential_format'])}
        except (ValueError, TypeError, AttributeError):
            raise RemoteError('密钥不符合 Bedrock 固定格式', category='configuration_error') from None
    if (config or {}).get('credential_format', {}).get('type') in ('vertex_gemini', 'vertex_claude'):
        try:
            config = {**config, 'credential_format': credential_wire_schema(key, config['credential_format'])}
        except (ValueError, TypeError, AttributeError):
            raise RemoteError('密钥不符合此 Vertex AI 服务的认证格式', category='configuration_error') from None
    fields, spec = config_fields(config, channel_type, **options)
    try:
        key = normalize_credential(key, SimpleNamespace(**spec))
    except (ValueError, TypeError, AttributeError):
        raise RemoteError('密钥不符合此渠道的固定格式', category='configuration_error') from None
    if not isinstance(remark, str) or len(remark) > 255:
        raise RemoteError('渠道备注不能超过 255 个字符', category='configuration_error')
    return {'name': name, 'key': key, 'models': ','.join(models), 'group': group,
            'test_model': models[0], 'tag': None, 'remark': remark,
            'param_override': None, 'header_override': None, **fields}


def wire_key_text(keys, schema):
    if schema.get('type') == 'vertex_json':
        return encode([json.loads(key) for key in keys])
    if any('\n' in key or '\r' in key for key in keys):
        raise RemoteError('多密钥文本不能包含内部换行', category='configuration_error')
    return '\n'.join(keys)


def validate_multikey_readback(remote, count):
    info = remote.get('channel_info')
    if (not isinstance(info, dict) or info.get('is_multi_key') is not True
            or type(info.get('multi_key_size')) is not int or info['multi_key_size'] != count
            or info.get('multi_key_mode') != 'random'):
        raise RemoteError('远端多密钥容器数量或随机分流配置不匹配，需人工核实', unknown=True,
                          category='configuration_mismatch')


def validate_readback(remote, config, proxy, channel_type, **options):
    """Fail unknown if a successful HTTP response silently dropped template data."""
    try:
        expected, _ = config_fields(config, channel_type, proxy=proxy, **options)
        for name, value in expected.items():
            actual = remote.get(name)
            if name in ('setting', 'settings'):
                wanted, received = json_object(value), json_object(actual)
                if any(received.get(k) != v for k, v in wanted.items()):
                    raise ValueError
                # An unset proxy/RPM toggle must not become remotely active.
                if name == 'setting' and (received.get('proxy', '') != wanted.get('proxy', '')
                                          or bool(received.get('rate_limit_enabled', False)) != bool(wanted.get('rate_limit_enabled', False))):
                    raise ValueError
            elif name in ('model_mapping', 'status_code_mapping'):
                if json_object(actual) != json_object(value):
                    raise ValueError
            elif name in ('base_url', 'openai_organization', 'other'):
                if (actual or '') != (value or ''):
                    raise ValueError
            elif type(actual) is not type(value) or actual != value:
                raise ValueError
    except (RemoteError, ValueError, TypeError, AttributeError):
        raise RemoteError('远端渠道配置与提交模板不一致，需人工核实', unknown=True,
                          category='configuration_mismatch') from None
