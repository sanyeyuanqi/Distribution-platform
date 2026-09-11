"""Resolve upload destinations from administrator templates without exposing secrets."""
import json
import secrets
import string
from datetime import UTC
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .adapters import supports_adapter
from .auth import redis_call
from .catalog_policy import (
    catalog_configuration_issues,
    catalog_default_base_url,
    catalog_format_spec,
    catalog_model_mapping,
    is_catalog_category,
    simplified_template_config,
)
from .channel_service import (
    UploadInput,
    channel_snapshot,
    existing_key_channels,
    parse_rows,
    supported_format,
    target_issues,
)
from .config import settings
from .credential_containers import (
    channel_partitions,
    multikey_supported,
    partition_entries,
    partition_for,
    primary_credential,
)
from .db import utcnow
from .distribution_tombstones import locally_deleted
from .models import Category, CredentialFormat, Site, SiteUploadTemplate
from .models_channels import Distribution, Task, UploadGroup
from .newapi_formats import (
    BEDROCK_DEFAULT_REGION,
    BEDROCK_INPUT_CONTRACT,
    format_default_models,
    validate_aws_claude_base_url,
)
from .security import decrypt, fingerprint
from .template_settings import AccountInfo, ChannelConfig, config_defaults


class SimpleUploadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category_id: str
    credentials: str = Field(min_length=1, max_length=200000)
    remarks: str = Field(default='', max_length=100000)
    proxies: str = Field(default='', max_length=100000)
    upload_mode: Literal['single', 'batch'] = 'batch'
    format_id: str | None = None
    models: list[str] | None = Field(default=None, min_length=1, max_length=200)
    rpm_enabled: bool | None = Field(default=None, strict=True)
    rpm_limit: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    account_info: AccountInfo | None = None
    inventory: bool = Field(default=False, strict=True)
    batch_token: str | None = Field(default=None, min_length=8, max_length=120)
    api_base_url: str | None = Field(default=None, max_length=1000)

    @field_validator('api_base_url')
    @classmethod
    def api_url(cls, value):
        return validate_aws_claude_base_url(value) if value else None

    @field_validator('models')
    @classmethod
    def model_list(cls, values):
        return UploadInput.model_list(values) if values is not None else None


class SimpleSubmitInput(SimpleUploadInput):
    idempotency_key: str = Field(min_length=8, max_length=120)
    configuration_revision: str | None = Field(default=None, min_length=16, max_length=128)


def batch_fingerprint(user, category_id, batch_token):
    return fingerprint(json.dumps(['upload-batch-v1', user.id, category_id, batch_token],
                                  ensure_ascii=False, separators=(',', ':')))


def find_batch_task(db, user, category_id, batch_token):
    if not batch_token:
        return None
    identity = batch_fingerprint(user, category_id, batch_token)
    return db.scalar(select(Task).where(Task.actor_id == user.id,
        Task.snapshot['batch_fingerprint'].as_string() == identity).limit(1))


LABEL_CACHE_SECONDS = 7 * 24 * 60 * 60
_LABEL_ALPHABET = string.ascii_lowercase + string.digits


def upload_type_code(category, fmt=None):
    variant = template_variant(category, fmt)
    types = {
        'AWS': {'bedrock': 'AWS-bedrock', 'aws_claude': 'AWS-claude'},
        'Azure': {'azure_gpt': 'Azure-openai', 'azure_claude': 'Azure-claude'},
        'Google': {'ai_studio_gemini': 'Google-ai-studio', 'vertex_gemini': 'Google-vertex-gemini',
                   'vertex_claude': 'Google-vertex-claude'},
    }
    if category.family in types:
        mapping = types[category.family]
        return mapping.get(variant, next(iter(mapping.values())))
    return category.family + '-api-key'


def new_upload_group_tag(db, user, category, fmt=None):
    """Reserve a short unique label; a reservation never changes business rows."""
    if not is_catalog_category(category) or type(user.display_id) is not int or user.display_id < 1:
        raise HTTPException(422, '账号编号或上传分类不可用，请刷新后重试')
    stamp = utcnow().replace(tzinfo=UTC).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H:%M:%S')
    prefix = f'{user.display_id}-{upload_type_code(category, fmt)}-{stamp}-'
    for _ in range(32):
        tag = prefix + ''.join(secrets.choice(_LABEL_ALPHABET) for _ in range(4))
        if db.scalar(select(UploadGroup.id).where(UploadGroup.tag == tag)):
            continue
        if redis_call('set', 'upload-label-tag:v3:' + tag, '1', nx=True, ex=LABEL_CACHE_SECONDS):
            return tag
    raise HTTPException(409, '自动标签暂时无法生成，请重试')


def batch_group_tag(db, user, category, batch_token, fmt=None):
    if not batch_token or not is_catalog_category(category):
        return None
    prior = find_batch_task(db, user, category.id, batch_token)
    if prior and prior.snapshot.get('group_tag'):
        return prior.snapshot['group_tag']
    key = f'upload-label:v3:{batch_fingerprint(user, category.id, batch_token)}:{upload_type_code(category, fmt)}'
    for _ in range(3):
        cached = redis_call('get', key)
        if cached:
            redis_call('expire', key, LABEL_CACHE_SECONDS)
            redis_call('expire', 'upload-label-tag:v3:' + cached, LABEL_CACHE_SECONDS)
            return cached
        tag = new_upload_group_tag(db, user, category, fmt)
        if redis_call('set', key, tag, nx=True, ex=LABEL_CACHE_SECONDS):
            return tag
        # Another request owns the batch winner. Read it rather than replacing
        # or deleting its entry; unused reservations expire on their own.
    raise HTTPException(409, '自动标签正在生成，请重试')


def template_config(template):
    """Version plus explicit fields protects against stale or out-of-band changes."""
    return {**{key: getattr(template, key) for key in (
        'id', 'version', 'site_id', 'category_id', 'format_id', 'models', 'routing_group', 'remark')},
        'channel_config': template.channel_config or {},
        # An additive empty default must not invalidate older frozen jobs.
        **({'model_mapping': dict(template.model_mapping)} if getattr(template, 'model_mapping', None) else {}),
        'proxy_fingerprint': fingerprint(template.proxy_encrypted) if template.proxy_encrypted else None}


def upload_settings(payload):
    """Persist only user overrides; submitted proxy plaintext never enters JSON."""
    values = payload.model_dump(mode='json', exclude_unset=True,
        exclude={'category_id', 'credentials', 'remarks', 'proxies', 'upload_mode', 'format_id',
                 'idempotency_key', 'configuration_revision', 'batch_token'})
    if not values.get('inventory'):
        values.pop('inventory', None)
    return {k: v for k, v in values.items() if v is not None or k == 'rpm_limit'}


def intersect_template_models(template_models, key_models=None):
    """Keep receiver order; an omitted legacy Key scope means all template models."""
    def normalize(values):
        if not isinstance(values, list) or len(values) > 200 or any(not isinstance(value, str) for value in values):
            raise ValueError('模板或 Key 模型范围格式无效')
        return UploadInput.model_list(values)

    receiving = normalize(template_models)
    if key_models is None:
        return receiving
    selected = set(normalize(key_models))
    return [model for model in receiving if model in selected]


def effective_template(template, overrides=None, channel_proxy=None, *, category=None, fmt=None, credential=None):
    overrides = overrides or {}
    config = config_defaults(simplified_template_config(template.channel_config))
    config['base_url'] = catalog_default_base_url(category)
    kind = (fmt.schema_config or {}).get('type') if fmt else None
    if fmt and (fmt.schema_config or {}).get('remote_type') == 41:
        config['other'] = '{"default":"global"}'
    if credential is not None and kind in ('azure_gpt', 'azure_claude'):
        from .azure_credentials import azure_credential_fields
        config.update(azure_credential_fields(credential, kind)['channel_config'])
    if overrides.get('inventory') is True:
        config['status'] = 2
    if overrides.get('api_base_url'):
        config['base_url'] = validate_aws_claude_base_url(overrides['api_base_url'])
    for name in ('rpm_enabled', 'rpm_limit'):
        if name in overrides and (overrides[name] is not None or name == 'rpm_limit'):
            config[name] = overrides[name]
    if overrides.get('account_info') is not None:
        config['account_info'] = {**config['account_info'], **overrides['account_info']}
    config = ChannelConfig.model_validate(config).model_dump(mode='json')
    models = intersect_template_models(template.models, overrides.get('models'))
    config['model_mapping'] = catalog_model_mapping(category, models, config['model_mapping'])
    if category and category.family == 'Google' and fmt:
        from .google_services import google_model_mapping
        config['model_mapping'].update(google_model_mapping(template_variant(category, fmt, template.models), models))
    # Custom targets override wire aliases only for the final receiving scope.
    config['model_mapping'].update({model: target for model, target in (getattr(template, 'model_mapping', None) or {}).items()
                                    if model in models})
    proxy = channel_proxy
    return {'models': models, 'channel_config': config,
            'config_explicit': bool(config != config_defaults() or overrides or proxy),
            'proxy_source': 'channel' if channel_proxy else None,
            'proxy_fingerprint': fingerprint(proxy) if proxy else None, 'proxy_requested': bool(proxy),
            'rpm_enabled': config['rpm_enabled'], 'rpm_limit': config['rpm_limit'],
            'enable_strategy': 'enabled' if config['status'] == 1 else 'disabled'}


def same_format_type(a, b):
    return bool(a and b and a.category_id == b.category_id
                and (a.schema_config or {}).get('remote_type', 1) == (b.schema_config or {}).get('remote_type', 1))


def template_variant(category, fmt, models=None):
    if category and category.family == 'Google' and fmt:
        from .google_services import google_service_variant
        return google_service_variant(fmt.schema_config, models)
    if category and category.family == 'AWS' and fmt and isinstance(fmt.schema_config, dict):
        if (fmt.schema_config or {}).get('remote_type') == 33:
            return 'bedrock'
        if (fmt.schema_config or {}).get('type') == 'aws_claude':
            return 'aws_claude'
    if category and category.family == 'Azure' and fmt and isinstance(fmt.schema_config, dict):
        if fmt.schema_config.get('type') == 'azure_claude':
            return 'azure_claude'
        if fmt.schema_config.get('remote_type') == 3:
            return 'azure_gpt'
    return ''


def configured_template_models(templates, sites, category, formats):
    """Names-only preview; never grants upload permission or selects a target."""
    names = []
    for template in templates:
        site, fmt = sites.get(template.site_id), formats.get(template.format_id)
        if not site or site.archived or not supports_adapter(site.adapter) or not supported_format(category, fmt):
            continue
        models = template.models
        if (not isinstance(models, list) or len(models) > 200
                or any(not isinstance(model, str) or not model or model != model.strip() or len(model) > 200
                       or len(model.encode('utf-8')) > 255 or ',' in model
                       or any(ord(char) < 32 or ord(char) == 127 for char in model) for model in models)):
            continue
        names.extend(models)
    return list(dict.fromkeys(names))


def aws_url_issues(fmt, config, *, required=False):
    if not fmt or (fmt.schema_config or {}).get('type') != 'aws_claude':
        return []
    value = (config or {}).get('base_url')
    if not value:
        return ['Claude 代理需要填写本批凭据对应的 API 地址'] if required else []
    try:
        validate_aws_claude_base_url(value)
    except ValueError as exc:
        return [str(exc)]
    return []


def distribution_template_snapshot(channel, fmt, site, template, *, category=None, partition_key='', partition_proxy_encrypted=None):
    config = template_config(template)
    multiple = getattr(channel, 'key_mode', 'single') == 'multiple'
    part = partition_for(channel, fmt, partition_key, site=site) if multiple else None
    proxy = partition_proxy_encrypted if multiple else channel.proxy_encrypted
    effective = effective_template(template, channel.upload_settings, proxy, category=category,
                                   fmt=fmt, credential=primary_credential(channel, fmt, partition_key, site=site))
    if multiple:
        effective['proxy_source'] = 'partition'
    if not effective['models']:
        raise ValueError('模板接收模型与 Key 模型范围没有交集，不能创建分发')
    note = channel.remark or (part['entries'][0]['remark'] if part else '') or template.remark
    history = {**config, 'effective_remark': note, 'effective_channel_config': effective['channel_config'],
               'effective_proxy_fingerprint': effective['proxy_fingerprint']}
    snap = channel_snapshot(channel, fmt, site, partition_key=partition_key)
    if 'bedrock_api_key_sdk' in snap:
        history['bedrock_api_key_sdk'] = snap['bedrock_api_key_sdk']
    snap.update(**effective, routing_group=template.routing_group, remark=note,
                channel_type=(fmt.schema_config or {}).get('remote_type', 1),
                upload_settings_snapshot=channel.upload_settings or {},
                channel_proxy_fingerprint=fingerprint(channel.proxy_encrypted) if channel.proxy_encrypted else None,
                upload_template_id=template.id, template_version=template.version, template_config=config)
    return history, snap


def template_issues(template, site, category, fmt, *, include_availability=True, remark_length=None,
                    effective=None):
    issues = []
    if not site or site.archived:
        return ['站点不存在或已归档']
    if not supported_format(category, fmt):
        issues.append('分类或凭据格式已停用、尚未实现或不匹配')
    if template_variant(category, fmt, template.models) == 'vertex_legacy':
        issues.append('旧 Vertex 模板的服务用途不明确，请分别建立 Gemini 或 Claude 模板')
    try:
        effective = effective or effective_template(template, category=category, fmt=fmt)
    except ValueError:
        return [*issues, '模板渠道参数或 RPM 设置无效']
    if not effective['models']:
        issues.append('模板尚未配置模型')
    issues.extend(catalog_configuration_issues(category, effective['channel_config']))
    issues.extend(aws_url_issues(fmt, effective['channel_config']))
    if fmt:
        issues.extend(target_issues(site, fmt, effective['models'], routing_group=template.routing_group,
                      remark_length=len(template.remark) if remark_length is None else remark_length,
                      proxy=effective['proxy_requested'], rpm=effective['rpm_enabled'],
                      channel_config=effective['channel_config']))
    if category and category.family == 'Azure' and not effective['channel_config'].get('base_url'):
        # The resource is supplied separately by each Key, never by the template.
        issues = [issue for issue in issues if issue != '此渠道类型必须配置 API 地址']
    if not include_availability:
        issues = [i for i in issues if i != '站点已停用或归档']
    return issues


def _resolve_templates(db, category_id, rows=None, payload=None, *, variant=None, legacy_channel=None):
    category = db.get(Category, category_id)
    templates = list(db.scalars(select(SiteUploadTemplate).where(
        SiteUploadTemplate.category_id == category_id).order_by(SiteUploadTemplate.created_at, SiteUploadTemplate.id)))
    sites = {s.id: s for s in db.scalars(select(Site).where(Site.id.in_([t.site_id for t in templates])))}
    formats = {f.id: f for f in db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == category_id))}
    if variant is not None:
        templates = [t for t in templates if template_variant(category, formats.get(t.format_id), t.models) == variant]
        historical_ids = {t.format_id for t in templates}
        if legacy_channel is not None:
            historical_ids.add(legacy_channel.format_id)
        formats = {key: fmt for key, fmt in formats.items()
                   if template_variant(category, fmt) == variant or key in historical_ids}
        sites = {key: site for key, site in sites.items() if key in {t.site_id for t in templates}}
    targets, skipped, public_targets, errors = [], [], [], []
    if not is_catalog_category(category) or not category.active:
        errors.append('分类不存在或已停用')
    active_format_ids = {t.format_id for t in templates if t.enabled and sites.get(t.site_id) and not sites[t.site_id].archived}
    active_wires = {(formats[fid].schema_config or {}).get('remote_type') for fid in active_format_ids if fid in formats}
    if len(active_format_ids) > 1 and variant != 'bedrock' and len(active_wires) != 1:
        errors.append('该分类的站点模板使用了不同凭据格式，请联系管理员统一模板格式')
    fmt = formats.get(next(iter(active_format_ids))) if len(active_format_ids) == 1 else None
    if variant is not None:
        # Administrators choose the definition. Only legacy split Bedrock
        # templates fall back to the merged parser for mixed-key uploads.
        input_kind = {'bedrock': 'aws_bedrock', 'ai_studio_gemini': 'api_key'}.get(variant, variant)
        if fmt is None or not catalog_format_spec(category, fmt) or (fmt.schema_config or {}).get('type') != input_kind:
            candidates = [f for f in formats.values() if supported_format(category, f) and catalog_format_spec(category, f)
                          and (f.schema_config or {}).get('type') == input_kind]
            fmt = next((f for f in candidates if f.id in active_format_ids), next(iter(candidates), fmt))
    if fmt is not None and not catalog_format_spec(category, fmt):
        fmt = next((f for f in formats.values() if supported_format(category, f)
                    and catalog_format_spec(category, f) and same_format_type(fmt, f)), None)
    default_fmt = fmt
    available_formats = [f for f in formats.values() if supported_format(category, f) and catalog_format_spec(category, f)
                         and same_format_type(default_fmt, f) and (variant is None or template_variant(category, f) == variant)]
    if payload and payload.format_id:
        selected = formats.get(payload.format_id)
        historical = (legacy_channel is not None and category and category.family == 'Google'
                      and legacy_channel.category_id == category_id and legacy_channel.format_id == payload.format_id
                      and selected and supported_format(category, selected)
                      and template_variant(category, selected, legacy_channel.models) == variant)
        if selected not in available_formats and not historical:
            errors.append('所选凭据格式不属于此分类的可用格式')
            fmt = None
        else:
            fmt = selected
    overrides = upload_settings(payload) if payload else {}
    if fmt and overrides.get('models') is not None:
        from .google_services import service_model_issues
        errors.extend(service_model_issues(fmt.schema_config, overrides['models']))
    if overrides.get('api_base_url') and (not fmt or (fmt.schema_config or {}).get('type') != 'aws_claude'):
        errors.append('API 地址仅用于 Claude 代理 (api.aws) 上传')
    public_models = list(dict.fromkeys(model for t in templates if t.enabled and sites.get(t.site_id)
        and sites[t.site_id].enabled and not sites[t.site_id].archived for model in t.models))
    configured_models = configured_template_models(templates, sites, category, formats)
    if overrides.get('models') and not any(m in public_models for m in overrides['models']):
        errors.append('所选 Key 模型列表与当前站点模板没有交集')
    for template in templates:
        site = sites.get(template.site_id)
        name = site.name if site else '已移除站点'
        if not template.enabled or not site or site.archived or not site.enabled:
            reason = '模板已停用' if not template.enabled else '站点已归档' if not site or site.archived else '站点分发已停用'
            skipped.append({'id': template.site_id, 'name': name, 'reason': reason})
            continue
        local_fmt = fmt or formats.get(template.format_id)
        try:
            effective = effective_template(template, overrides, category=category, fmt=local_fmt)
        except ValueError:
            effective = None
        if effective and not effective['models'] and overrides.get('models'):
            skipped.append({'id': site.id, 'name': name, 'reason': '此站点模板不包含所选模型'})
            continue
        if effective and rows and any(r.get('proxy') for r in rows):
            effective['proxy_requested'] = True
        note_length = max((len(r['remark'] or template.remark) for r in rows), default=len(template.remark)) if rows is not None else None
        issues = (template_issues(template, site, category, local_fmt, remark_length=note_length, effective=effective)
                  if effective else ['渠道参数或 RPM 设置无效'])
        url_issues = aws_url_issues(local_fmt, effective['channel_config'], required=payload is not None) if effective else []
        issues.extend(issue for issue in url_issues if issue not in issues)
        if rows and effective and local_fmt and (local_fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude', 'vertex_gemini', 'vertex_claude'):
            for row in rows:
                if row.get('status') != 'valid':
                    continue
                try:
                    row_config = effective_template(template, overrides, category=category, fmt=local_fmt,
                                                    credential=row['key'])
                    row_issues = target_issues(site, local_fmt, row_config['models'], routing_group=template.routing_group,
                                              channel_config=row_config['channel_config'],
                                              wire_schema=partition_entries([row], local_fmt)[0]['wire_format_schema'])
                except ValueError:
                    row_issues = ['Azure 凭据格式无效']
                issues.extend(issue for issue in row_issues if issue not in issues)
        # Publish fixed availability labels, never private model/group values or
        # arbitrary verification/provider error strings.
        safe_issues = {'当前卖家契约未定义代理参数', '当前卖家契约未定义 RPM 执行参数',
                       '站点不支持所选凭据格式', '此站点版本不支持所选渠道类型', '渠道参数或 RPM 设置无效',
                       '此站点版本尚未验证 Vertex Claude API Key 支持，请使用服务账号 JSON',
                       '站点没有创建渠道权限', '站点尚未验证，请先验证站点',
                       '站点尚未验证渠道启用权限，不能使用创建后启用的模板', '模板尚未配置模型',
                       *catalog_configuration_issues(category), *url_issues}
        from .adapters.newapi_builds import GROUP_TOO_LONG_MESSAGE
        safe_issues.add(GROUP_TOO_LONG_MESSAGE)
        public_issues = [i for i in issues if i in safe_issues]
        if any(i not in safe_issues for i in issues):
            public_issues.append('站点模板配置不可用，请联系管理员检查')
        limit = (site.capabilities or {}).get('remark_max_length')
        if type(limit) is int and note_length is not None and note_length > limit:
            public_issues = list(dict.fromkeys([*public_issues, f'备注超过此站点的 {limit} 字限制']))
        public_targets.append({'id': site.id, 'name': name, 'compatible': not issues, 'issues': public_issues})
        targets.append((template, site))
    if not targets:
        errors.append('此分类暂无可分发的站点模板，请联系管理员配置并启用模板和站点')
    if active_format_ids and (fmt is None or not supported_format(category, fmt)):
        errors.append('此分类的凭据格式不可用')
    from .newapi_formats import format_descriptor
    try:
        defaults = config_defaults(targets[0][0].channel_config) if targets else config_defaults()
    except ValueError:
        defaults = config_defaults()
    public = {'category_id': category_id, 'category_name': category.family if category else '',
              'family': category.family if category else '', 'format_id': fmt.id if fmt else None,
              'format_name': catalog_format_spec(category, fmt)['name'] if catalog_format_spec(category, fmt) else '', 'target_count': len(targets),
              'models': public_models, 'configured_models': configured_models,
              'formats': [{**format_descriptor(f, category), 'id': f.id, 'version': f.version,
                           'schema_config': catalog_format_spec(category, f)['schema_config'],
                           'default_models': format_default_models(catalog_format_spec(category, f)['schema_config']),
                           'input_mode': format_descriptor(f, category)['input_mode']} for f in available_formats],
              'defaults': {k: defaults[k] for k in ('default_upload_mode', 'rpm_enabled', 'rpm_limit', 'account_info')},
              'targets': public_targets, 'skipped_targets': skipped, 'issues': errors,
              'ready': bool(targets) and not errors and all(t['compatible'] for t in public_targets)}
    revision_data = {'category': [category.id, category.active, category.family] if category else None,
        'templates': [{**template_config(t), 'enabled': t.enabled} for t in templates],
        'sites': [{k: getattr(s, k) for k in ('id', 'name', 'enabled', 'archived', 'adapter', 'base_url',
                   'seller_user_id', 'health', 'capabilities', 'verified_at')} for s in sorted(sites.values(), key=lambda s: s.id)],
        'formats': [{k: getattr(f, k) for k in ('id', 'enabled', 'code', 'version', 'category_id', 'schema_config')} for f in sorted(formats.values(), key=lambda f: f.id)]}
    if fmt and (fmt.schema_config or {}).get('type') == 'aws_bedrock':
        revision_data['credential_input'] = [BEDROCK_INPUT_CONTRACT, BEDROCK_DEFAULT_REGION]
        public['default_region'] = BEDROCK_DEFAULT_REGION
    public['configuration_revision'] = fingerprint(json.dumps(revision_data, sort_keys=True, ensure_ascii=False, default=str))
    if variant is not None:
        public['variant'] = variant
        if variant == 'aws_claude':
            public['api_base_url_required'] = True
    return public, targets, category, fmt


def resolve_templates(db, category_id, rows=None, payload=None, *, legacy_channel=None):
    category = db.get(Category, category_id)
    if not category or category.family not in ('AWS', 'Azure', 'Google'):
        return _resolve_templates(db, category_id, rows, payload)
    from .google_services import GOOGLE_VARIANTS
    variant_names = {'AWS': ('bedrock', 'aws_claude'), 'Azure': ('azure_gpt', 'azure_claude'),
                     'Google': GOOGLE_VARIANTS}[category.family]
    variants = [_resolve_templates(db, category_id, variant=name)
                for name in variant_names]
    selected_variant = next((item[0]['variant'] for item in variants if item[1]), variant_names[0])
    if payload and payload.format_id:
        selected_variant = template_variant(category, db.get(CredentialFormat, payload.format_id),
                                            legacy_channel.models if legacy_channel is not None else None)
        if not selected_variant:
            selected_variant = variant_names[0]  # Invalid explicit formats are rejected by the scoped resolver.
    result = _resolve_templates(db, category_id, rows, payload, variant=selected_variant, legacy_channel=legacy_channel)
    result[0]['variants'] = [item[0] for item in variants]
    return result


def conflict_row(row, reasons, channel=None):
    """Publish reason labels only; never include stored credentials/config values."""
    reasons = list(dict.fromkeys(reasons))
    row.update(status='conflict', reasons=reasons, message='；'.join(reasons))
    if channel:
        row['existing_channel_id'] = channel.id


def channel_conflict_reasons(channel, payload, overrides, *, format_matches, remark_matches, proxy_matches,
                             stored_settings):
    reasons = []
    if channel.archived:
        reasons.append('已有本地渠道已归档，不能通过重复上传自动恢复')
    if channel.upload_mode != 'template':
        reasons.append('原渠道不是通过站点模板上传，不能用本次上传覆盖')
    if channel.category_id != payload.category_id:
        reasons.append('本次上传分类与原渠道不同')
    if not format_matches:
        reasons.append('本次凭据格式与原渠道不同')
    if not remark_matches:
        reasons.append('本次备注与原渠道保存的备注不同')
    if not proxy_matches:
        reasons.append('本次代理设置与原渠道保存的设置不同')
    if stored_settings != overrides:
        old = stored_settings or {}
        changed = {key for key in old.keys() | overrides.keys()
                   if key not in old or key not in overrides or old[key] != overrides[key]}
        if 'models' in changed:
            reasons.append('本次选择的模型范围与原渠道不同')
        if 'inventory' in changed:
            reasons.append('本次入库存设置与原渠道不同')
        if changed - {'models', 'inventory'} or not changed:
            reasons.append('本次其他上传选项与原渠道不同')
    return reasons


def deletion_conflict_reasons(dist):
    if dist.status != 'deleted':
        return []
    if locally_deleted(dist):
        return ['已有分发已强制删除（本地），远端是否删除尚未确认，不能通过重复上传恢复']
    return ['已有远端分发已删除，不能通过重复上传重新创建']


def distribution_conflict_reasons(dist, template, effective, note, *, single=False):
    reasons = deletion_conflict_reasons(dist)
    if dist.upload_template_id != template.id:
        reasons.append('原分发关联的站点模板与当前模板不同')
    if dist.models != effective['models']:
        reasons.append('原分发模型与当前站点模板的有效模型范围不同')
    if dist.routing_group != template.routing_group:
        reasons.append('原分发的目标渠道分组与当前站点模板不同')
    if dist.template_snapshot.get('effective_remark') != note:
        reasons.append('原分发的生效备注与本次上传或当前模板不同')
    old_config = (dist.template_snapshot.get('effective_channel_config', config_defaults()) if single else
                  dist.template_snapshot.get('effective_channel_config'))
    if old_config != effective['channel_config']:
        reasons.append('原分发的生效渠道配置与当前站点模板不同')
    if single and dist.template_snapshot.get('effective_proxy_fingerprint') != effective['proxy_fingerprint']:
        reasons.append('原分发的生效代理设置与本次上传不同')
    return reasons


def prepare_simple_upload(db, user, payload, *, legacy_channel=None):
    _, _, _, fmt = resolve_templates(db, payload.category_id, payload=payload, legacy_channel=legacy_channel)
    # An unavailable format has no safe parser; avoid interpreting old JSON as raw keys.
    rows, errors = (parse_rows(payload.credentials, payload.remarks, payload.proxies, limit=settings.upload_limit,
                              fmt=fmt, upload_mode=payload.upload_mode) if fmt else ([], []))
    plan, targets, category, fmt = resolve_templates(db, payload.category_id, rows, payload, legacy_channel=legacy_channel)
    errors.extend(plan['issues'])
    overrides = upload_settings(payload)
    existing = {}
    for row in rows:
        if row.get('key'):
            matches = existing_key_channels(db, user.id, row['key'], fmt, category, models=overrides.get('models'))
            if len(matches) > 1:
                conflict_row(row, ['同一服务已有多条历史凭据记录，请联系管理员核对'])
            elif matches:
                existing[row['fingerprint']] = matches[0]
    distributions = {}
    for dist in db.scalars(select(Distribution).where(Distribution.channel_id.in_([c.id for c in existing.values()]))):
        distributions.setdefault(dist.channel_id, {})[(dist.site_id, dist.partition_key)] = dist
    work_rows = []
    for row in rows:
        if row['status'] != 'valid':
            continue
        channel = existing.get(row['fingerprint'])
        if channel:
            if getattr(channel, 'key_mode', 'single') == 'multiple':
                from .credential_containers import channel_entries
                from .newapi_formats import credential_fingerprint
                entry = next((e for e in channel_entries(channel) if credential_fingerprint(e['key'], fmt) == row['fingerprint']), None)
                reasons = channel_conflict_reasons(channel, payload, overrides,
                    format_matches=channel.format_id == fmt.id, stored_settings=channel.upload_settings,
                    remark_matches=not entry or entry['remark'] == row['remark'],
                    proxy_matches=not entry or entry['proxy'] == row.get('proxy', ''))
                if not entry:
                    reasons.append('密钥成员记录与原容器不一致，请联系管理员核对')
                current = distributions.get(channel.id, {})
                if reasons:
                    # Archive/format conflicts used to short-circuit this branch.
                    # Still report removed target rows without reparsing the old
                    # container under a potentially incompatible new format.
                    site_ids = {site.id for _, site in targets}
                    for dist in current.values():
                        if dist.site_id in site_ids:
                            reasons.extend(deletion_conflict_reasons(dist))
                    conflict_row(row, reasons, channel)
                    continue
                for template, site in targets:
                    for part in channel_partitions(channel, fmt, site=site):
                        dist = current.get((site.id, part['partition_key']))
                        if not dist:
                            continue
                        effective = effective_template(template, overrides, category=category, fmt=fmt,
                                                       credential=part['entries'][0]['key'])
                        note = channel.remark or part['entries'][0]['remark'] or template.remark
                        reasons.extend(distribution_conflict_reasons(dist, template, effective, note))
                if reasons:
                    conflict_row(row, reasons, channel)
                    continue
                missing = [site.id for _, site in targets
                           if any((site.id, p['partition_key']) not in current for p in channel_partitions(channel, fmt, site=site))]
                row.update(existing_channel_id=channel.id, missing_site_ids=list(dict.fromkeys(missing)))
                if not missing:
                    row.update(status='already_distributed', message='该密钥所属容器已包含所有目标，失败项请从原任务重试')
                    continue
                row.update(status='redistribute', message='补分发已有完整密钥容器，不重复创建本地渠道')
                work_rows.append(row)
                continue
            stored_fmt = db.get(CredentialFormat, channel.format_id)
            compatible_bedrock = (template_variant(category, fmt) == 'bedrock'
                                  and same_format_type(stored_fmt, fmt))
            compatible_google = (category.family == 'Google' and same_format_type(stored_fmt, fmt)
                and template_variant(category, stored_fmt, channel.models) == template_variant(category, fmt, channel.models))
            reasons = channel_conflict_reasons(channel, payload, overrides,
                format_matches=fmt is not None and (channel.format_id == fmt.id or compatible_bedrock or compatible_google),
                stored_settings=channel.upload_settings or {}, remark_matches=channel.remark == row['remark'],
                proxy_matches=(decrypt(channel.proxy_encrypted) if channel.proxy_encrypted else '') == row.get('proxy', ''))
            current = distributions.get(channel.id, {})
            for template, site in targets:
                dist = current.get((site.id, ''))
                effective = effective_template(template, overrides, channel.proxy_encrypted, category=category,
                                               fmt=stored_fmt, credential=decrypt(channel.key_encrypted))
                if dist:
                    reasons.extend(distribution_conflict_reasons(dist, template, effective,
                                                                row['remark'] or template.remark, single=True))
            if reasons:
                conflict_row(row, reasons, channel)
                continue
            row.update(existing_channel_id=channel.id, missing_site_ids=[site.id for _, site in targets if (site.id, '') not in current])
            if not row['missing_site_ids']:
                row.update(status='already_distributed', message='该密钥已包含所有模板目标，失败记录请从原任务重试')
                continue
            row.update(status='redistribute', message='保留已有渠道，仅补分发新增的模板站点')
        else:
            row['missing_site_ids'] = [site.id for _, site in targets]
        work_rows.append(row)
    new_rows = [row for row in work_rows if not row.get('existing_channel_id')]
    old_ids = {row.get('existing_channel_id') for row in rows if row.get('existing_channel_id')}
    if new_rows and old_ids:
        errors.append('本批同时包含已有容器成员和新密钥，请单独上传新密钥；不会改写已有容器')
    parts = []
    for _, site in targets:
        for part in partition_entries(new_rows, fmt, multiple=len(new_rows) > 1, site=site) if new_rows else []:
            parts.append(part)
            if part['key_count'] > 1 and not multikey_supported(site):
                errors.append('目标站点尚未验证多密钥容器支持，请联系管理员重新验证站点')
    for channel_id in {row['existing_channel_id'] for row in work_rows if row.get('existing_channel_id')}:
        channel = next(c for c in existing.values() if c.id == channel_id)
        for _, site in targets:
            parts.extend(part for part in channel_partitions(channel, db.get(CredentialFormat, channel.format_id), site=site)
                         if (site.id, part['partition_key']) not in distributions.get(channel.id, {}))
    invalid = any(r['status'] in ('invalid', 'conflict') for r in rows)
    if not work_rows and not invalid and not errors:
        errors.append('没有新增密钥或待补分发项')
    result = {**plan, 'errors': errors, 'original_count': len(rows), 'valid_count': len(work_rows),
              'duplicate_count': sum(r['status'] in ('duplicate', 'already_distributed', 'redistribute') for r in rows),
              'conflict_count': sum(r['status'] == 'conflict' for r in rows),
              'rows': [{k: v for k, v in r.items() if k not in ('key', 'fingerprint', 'proxy', 'missing_site_ids')} for r in rows],
              'can_submit': bool(work_rows) and plan['ready'] and not errors and not invalid,
              'can_submit_partial': False, 'enable_strategy': 'disabled' if payload.inventory else 'template',
              'inventory': payload.inventory, 'group_tag': batch_group_tag(db, user, category, payload.batch_token, fmt),
              'template_mode': True,
              'channel_count': int(bool(new_rows)) + len({row['existing_channel_id'] for row in work_rows if row.get('existing_channel_id')}),
              'partition_count': len({part['partition_key'] for part in parts}), 'remote_channel_count': len(parts),
              'partition_summary': [{'label': part['partition_label'], 'key_count': part['key_count'],
                                     'target_count': 1} for part in parts]}
    return result, work_rows, targets, category, fmt
