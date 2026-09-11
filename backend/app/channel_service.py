from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis import Redis, RedisError
from sqlalchemy import func, or_, select

from .adapters import supports_adapter
from .auth import assert_owner
from .catalog_policy import (
    catalog_configuration_issues,
    catalog_format_spec,
    implemented_catalog_format,
)
from .config import settings
from .db import uid, utcnow
from .models import Category, CredentialFormat, Site, SiteUploadTemplate
from .models_channels import Channel, ChannelCredential, Distribution, Task, TaskItem
from .newapi_formats import (
    credential_fingerprint,
    credential_hint,
    credential_records,
    credential_wire_schema,
    format_spec,
    normalize_credential,
)
from .routing_groups import routing_group_names
from .security import decrypt, fingerprint


def api_json(value):
    """Database timestamps are UTC, so responses explicitly include the UTC offset."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z') if value.tzinfo is None else value.isoformat()
    if isinstance(value, dict):
        return {key: api_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [api_json(item) for item in value]
    return value


class UploadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category_id: str
    format_id: str
    credentials: str = Field(min_length=1, max_length=200000)
    remarks: str = Field(default='', max_length=100000)
    proxies: str = Field(default='', max_length=100000)
    models: list[str] = Field(default_factory=list, max_length=200)
    declaration: str = Field(default='', max_length=4000)
    rpm_enabled: bool = False
    rpm_limit: int | None = Field(default=None, ge=1, le=1000000)
    enable_strategy: str = 'disabled'

    @field_validator('enable_strategy')
    @classmethod
    def strategy(cls, value):
        if value not in ('disabled', 'test_then_enable'):
            raise ValueError('启用策略无效')
        return value

    @field_validator('models')
    @classmethod
    def model_list(cls, values):
        if any(not x.strip() or len(x) > 200 or ',' in x or '\n' in x for x in values):
            raise ValueError('模型名称不能为空、过长或包含逗号/换行')
        return list(dict.fromkeys(x.strip() for x in values))


class SubmitInput(UploadInput):
    idempotency_key: str = Field(min_length=8, max_length=120)
    allow_partial: bool = False


def supported_format(category, fmt):
    return bool(implemented_catalog_format(category, fmt) and category.active and fmt.enabled)


def normalize_key(value, fmt=None):
    return normalize_credential(value, fmt)


def existing_key_channels(db, owner_id, key, fmt, category, *, models=None):
    """New Vertex identities are service-scoped; recognize immutable legacy IDs."""
    from types import SimpleNamespace

    from .google_services import google_service_variant

    identity = credential_fingerprint(key, fmt)
    candidates = {identity}
    service = google_service_variant(fmt.schema_config, models) if fmt and category and category.family == 'Google' else ''
    if service in ('vertex_gemini', 'vertex_claude'):
        candidates.update({fingerprint(key), credential_fingerprint(key,
            SimpleNamespace(schema_config={'type': service, 'remote_type': 41}))})
    members = select(ChannelCredential.channel_id).where(ChannelCredential.owner_id == owner_id,
                                                         ChannelCredential.fingerprint.in_(candidates))
    rows = db.scalars(select(Channel).where(Channel.owner_id == owner_id,
                      or_(Channel.fingerprint.in_(candidates), Channel.id.in_(members))))
    result = []
    for row in rows:
        member_match = db.scalar(select(ChannelCredential.id).where(ChannelCredential.channel_id == row.id,
                                                          ChannelCredential.fingerprint == identity).limit(1))
        if row.fingerprint == identity or member_match:
            result.append(row)
        elif category and row.category_id == category.id:
            old_fmt = db.get(CredentialFormat, row.format_id)
            if old_fmt and google_service_variant(old_fmt.schema_config, row.models) == service:
                result.append(row)
    return result


def parse_rows(credentials, remarks='', proxies='', limit=100, fmt=None, upload_mode='batch'):
    """Bind original rows first; trim only credential-block boundary blank lines."""
    records, line_numbers = credential_records(credentials, fmt)
    errors = []
    if not records:
        return [], ['至少输入一条有效凭据']
    if len(records) > limit:
        errors.append(f'单次最多上传 {limit} 条凭据')
    if upload_mode not in ('single', 'batch'):
        errors.append('请选择单密钥或批量上传')
    if upload_mode == 'single' and len(records) != 1:
        errors.append('单密钥模式只允许一条凭据，请切换批量上传')

    def bind(value, label):
        if not value:
            return [''] * len(records)
        values = value.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        if len(values) == 1:
            return [values[0].strip()] * len(records)
        if len(values) != len(records):
            errors.append(f'{label}行数错误：需要 {len(records)} 行，实际 {len(values)} 行')
            return [''] * len(records)
        return [v.strip() for v in values]

    notes, addresses = bind(remarks, '备注'), bind(proxies, '代理')
    rows, seen = [], {}
    for index, (value, note, proxy) in enumerate(zip(records, notes, addresses)):
        line = line_numbers[index]
        row = {'line': line, 'key': '', 'key_hint': '', 'remark': note, 'proxy': proxy,
               'proxy_hint': '', 'status': 'valid', 'message': ''}
        try:
            key = normalize_key(value, fmt)
            row.update(key=key, key_hint=credential_hint(key, fmt), fingerprint=credential_fingerprint(key, fmt))
            if len(note) > 4000:
                raise ValueError('备注不能超过 4000 个字符')
            if proxy:
                try:
                    parsed = urlsplit(proxy)
                    host, port = parsed.hostname, parsed.port
                except ValueError:
                    raise ValueError('代理需要合法协议、主机及端口') from None
                if parsed.scheme not in ('http', 'https', 'socks5') or not host or not port:
                    raise ValueError('代理需要合法协议、主机及端口')
                if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                    raise ValueError('代理地址不允许包含路径、查询或片段')
                row['proxy_hint'] = f'{parsed.scheme}://***@{parsed.hostname}:{parsed.port}' if parsed.username else f'{parsed.scheme}://{parsed.hostname}:{parsed.port}'
            if row['fingerprint'] in seen:
                first = seen[row['fingerprint']]
                if (note, proxy) == (first['remark'], first['proxy']):
                    row.update(status='duplicate', message=f'与第 {first["line"]} 行配置一致，保留首次')
                else:
                    row.update(status='conflict', message=f'与第 {first["line"]} 行凭据相同但配置不同')
                    first.update(status='conflict', message=f'与第 {line} 行凭据相同但配置不同')
            else:
                seen[row['fingerprint']] = row
        except ValueError as exc:
            row.update(status='invalid', message=str(exc))
        rows.append(row)
    return rows, errors


def target_issues(site, fmt, models, *, proxy=False, rpm=False, strategy='disabled', remark_length=0, routing_group=None,
                  channel_config=None, wire_schema=None):
    from .site_verification import verification_state

    cap = site.capabilities or {}
    from .google_services import service_model_issues
    issues = service_model_issues(fmt.schema_config, models)
    if not site.enabled or site.archived:
        issues.append('站点已停用或归档')
    if not supports_adapter(site.adapter):
        issues.append('站点适配器尚未实现')
    verification, error = verification_state(site)
    if verification != 'verified':
        issues.append('站点验证失败：' + error['message'] if error else '站点尚未验证，请先验证站点')
        # Missing capabilities are unknown, not evidence that every provider,
        # model, group and write parameter is unsupported. This still blocks all
        # submissions and enabled-template changes until verification succeeds.
        return issues
    if cap.get('create') != 'supported' or cap.get('can_write') is not True:
        issues.append('站点没有创建渠道权限')
    if ((fmt.schema_config or {}).get('type') == 'vertex_claude'
            and (wire_schema or {}).get('type') == 'vertex_api_key'
            and cap.get('vertex_claude_api_key') != 'supported'):
        issues.append('此站点版本尚未验证 Vertex Claude API Key 支持，请使用服务账号 JSON')
    supported_codes = set(cap.get('formats', []))
    aliases = {
        'newapi-33-aws-bedrock-v1': {'newapi-33-aws-ak-sk-v1', 'newapi-33-aws-api-key-v1'},
        'newapi-14-aws-claude-v1': {'newapi-14-api-key-v1'},
        'newapi-3-azure-gpt-v1': {'newapi-3-api-key-v1'},
        'newapi-14-azure-claude-v1': {'newapi-14-api-key-v1'},
        'newapi-41-vertex-gemini-v1': {'newapi-41-vertex-json-v1', 'newapi-41-vertex-api-key-v1'},
        'newapi-41-vertex-claude-v1': {'newapi-41-vertex-json-v1'},
    }
    spec = format_spec(fmt)
    wire_code = spec['wire_code'] if spec else None
    known_wire = (wire_code in aliases and aliases[wire_code] <= supported_codes
                  and cap.get('channel_config') == 'supported')
    if wire_code not in supported_codes and not known_wire:
        issues.append('站点不支持所选凭据格式')
    if (fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude') and cap.get('channel_config') != 'supported':
        issues.append('此站点未验证 Azure 资源地址与版本参数的支持能力')
    channel_type = (fmt.schema_config or {}).get('remote_type', 1)
    if 'channel_types' in cap and channel_type not in cap['channel_types']:
        issues.append('此站点版本不支持所选渠道类型')
    routing_group = (getattr(site, 'routing_group', None) or 'default') if routing_group is None else routing_group
    try:
        requested_groups = routing_group_names(routing_group)
        from .adapters.newapi_builds import group_length_issue, site_verified_build
        build = site_verified_build(site)
        if build and (issue := group_length_issue(build['id'], ','.join(requested_groups))):
            issues.append(issue)
        unavailable_groups = [group for group in requested_groups if group not in cap.get('groups', [])]
        if unavailable_groups:
            issues.append('站点不允许路由组 ' + '、'.join(unavailable_groups) + '，请选择已验证的可用组')
    except ValueError as exc:
        issues.append(str(exc))
    unsupported = [m for m in models if m not in cap.get('models', [])]
    if unsupported and cap.get('custom_models') is not True:
        issues.append('不支持的模型：' + ', '.join(unsupported))
    if type(cap.get('model_max_bytes')) is int and any(len(model.encode('utf-8')) > cap['model_max_bytes'] for model in models):
        issues.append(f'此站点模型名称不能超过 {cap["model_max_bytes"]} 个 UTF-8 字节')
    remark_limit = cap.get('remark_max_length')
    if type(remark_limit) is int and remark_length > remark_limit:
        issues.append(f'站点备注最多允许 {remark_limit} 个字符')
    if proxy and cap.get('proxy') != 'supported':
        issues.append('当前卖家契约未定义代理参数')
    if rpm and cap.get('rpm') != 'supported':
        issues.append('当前卖家契约未定义 RPM 执行参数')
    config = channel_config or {}
    if config.get('status', 2) == 1 and cap.get('can_toggle') is not True:
        issues.append('站点尚未验证渠道启用权限，不能使用创建后启用的模板')
    defaults = {'base_url': '', 'organization': '', 'status': 2, 'priority': 0, 'weight': 1, 'auto_ban': 1,
                'model_mapping': {}, 'status_code_mapping': {}, 'other': '', 'azure_responses_version': ''}
    if any(config.get(key, default) != default for key, default in defaults.items()) and cap.get('channel_config') != 'supported':
        issues.append('此站点未验证完整渠道模板参数的支持能力，请重新验证站点或调整模板')
    unsupported_config = cap.get('unsupported_config_fields') or []
    for field, message in (
            ('auto_ban', '此站点版本不支持自动禁用设置'),
            ('status_code_mapping', '此站点版本不支持状态码映射')):
        if field in unsupported_config and config.get(field, defaults[field]) != defaults[field]:
            issues.append(message)
    # All supported adapters share the same provider payload requirements.
    # Reject incomplete templates before enabling them or queuing remote writes.
    if channel_type in (3, 8, 59, 60) and not config.get('base_url'):
        issues.append('此渠道类型必须配置 API 地址')
    if channel_type == 41:
        import json
        try:
            regions = json.loads(config.get('other', ''))
            if (not isinstance(regions, dict) or not regions.get('default')
                    or any(not isinstance(region, str) or not region.strip() for region in regions.values())):
                raise ValueError
        except (ValueError, TypeError):
            issues.append('Vertex AI 需要在扩展参数中配置地区 JSON，例如 {"default":"us-central1"}')
    if channel_type in (18, 39, 49) and not config.get('other', '').strip():
        issues.append('此渠道类型需要扩展参数（版本、Cloudflare Account ID 或 Coze Bot ID）')
    if channel_type != 3 and config.get('azure_responses_version'):
        issues.append('Azure Responses API 版本仅适用于 Azure 渠道')
    if strategy == 'test_then_enable':
        issues.append('当前卖家契约未定义测试参数与响应，无法测试后启用')
    if site.adapter == 'spacex-hub-v1':
        from .adapters.spacex_hub import template_config_issues
        # Keep the business service here: Vertex Claude and Gemini share a
        # local wire type, but are different platforms in the Hub contract.
        issues.extend(template_config_issues(cap, fmt.schema_config, config,
                                            routing_group=routing_group, proxy=proxy))
    return issues


def prepare_upload(db, user, payload):
    category, fmt = db.get(Category, payload.category_id), db.get(CredentialFormat, payload.format_id)
    rows, errors = parse_rows(payload.credentials, payload.remarks, payload.proxies, settings.upload_limit, fmt=fmt)
    if db.scalar(select(SiteUploadTemplate.id).where(SiteUploadTemplate.category_id == payload.category_id).limit(1)):
        errors.append('此分类已由站点模板管理，请使用简化上传')
    if not supported_format(category, fmt) or not catalog_format_spec(category, fmt):
        errors.append('所选分类或格式未实现、已停用或不匹配')
    errors.extend(catalog_configuration_issues(category))
    models = payload.models or []
    if not models:
        errors.append('必须选择模型')
    if (not isinstance(models, list) or len(models) > 200
            or any(not isinstance(m, str) or not m or len(m) > 200 or ',' in m or '\n' in m for m in models)):
        errors.append('模型列表格式无效')
        models = []
    sites = list(db.scalars(select(Site).where(Site.enabled.is_(True), Site.archived.is_(False)).order_by(Site.created_at)))
    if not sites:
        errors.append('没有已启用的目标站点')
    targets = []
    for site in sites:
        issues = target_issues(site, fmt, models, proxy=any(r['proxy'] for r in rows),
                               rpm=payload.rpm_enabled, strategy=payload.enable_strategy,
                               remark_length=max((len(r['remark']) for r in rows), default=0)) if fmt else ['格式无效']
        targets.append({'id': site.id, 'name': site.name, 'compatible': not issues, 'issues': issues})
    existing = {}
    for row in rows:
        if row.get('key'):
            matches = existing_key_channels(db, user.id, row['key'], fmt, category, models=models)
            if len(matches) > 1:
                row.update(status='conflict', message='同一服务已有多条历史凭据记录，请联系管理员核对')
            elif matches:
                existing[row['fingerprint']] = matches[0]
    existing_targets = {}
    for channel_id, site_id in db.execute(select(Distribution.channel_id, Distribution.site_id)
                                         .where(Distribution.channel_id.in_([c.id for c in existing.values()]))):
        existing_targets.setdefault(channel_id, set()).add(site_id)
    work_rows = []
    for row in rows:
        if row['status'] != 'valid':
            continue
        channel = existing.get(row['fingerprint'])
        if channel:
            # Existing channel metadata and original group are never silently replaced.
            matches = (channel.upload_mode != 'template' and channel.category_id == payload.category_id and channel.format_id == payload.format_id
                       and channel.models == models and channel.remark == row['remark']
                       and channel.declaration == payload.declaration
                       and (decrypt(channel.proxy_encrypted) if channel.proxy_encrypted else '') == row['proxy'])
            if channel.archived or not matches:
                row.update(status='conflict', message='本人的已有渠道已归档或配置不同，请在渠道详情维护', existing_channel_id=channel.id)
                continue
            distributed = existing_targets.get(channel.id, set())
            row.update(existing_channel_id=channel.id, missing_site_ids=[s.id for s in sites if s.id not in distributed])
            if not row['missing_site_ids']:
                row.update(status='already_distributed', message='本人的已有渠道已包含所有目标；失败记录请从原任务重试')
                continue
            row.update(status='redistribute', message='保留原分组，仅补分发尚未包含的站点')
        else:
            row['missing_site_ids'] = [s.id for s in sites]
        work_rows.append(row)
    invalid = [r for r in rows if r['status'] in ('invalid', 'conflict')]
    has_work = any(t['compatible'] and t['id'] in r['missing_site_ids'] for r in work_rows for t in targets)
    if not work_rows and not invalid and not errors:
        errors.append('没有新增凭据或待补分发项，不会创建空任务')
    result = {'original_count': len(rows), 'valid_count': len(work_rows),
              'duplicate_count': sum(r['status'] in ('duplicate', 'already_distributed', 'redistribute') for r in rows),
              'conflict_count': sum(r['status'] == 'conflict' for r in rows),
              'rows': [{k: v for k, v in r.items() if k not in ('key', 'fingerprint', 'proxy', 'missing_site_ids')} for r in rows],
              'models': models, 'targets': targets, 'errors': errors,
              'can_submit': bool(work_rows) and not errors and not invalid and all(t['compatible'] for t in targets),
              'can_submit_partial': has_work and not errors and not invalid,
              'enable_strategy': payload.enable_strategy}
    return result, work_rows, sites, category, fmt


def notify_worker():
    # Redis is an acceleration signal; PostgreSQL remains the authoritative queue.
    try:
        Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).lpush('keyacross:work', '1')
    except RedisError:
        pass


def task_json(db, task, *, details=False, counts=None):
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.created_at))) if details else []
    if counts is None:
        counts = {}
        if details:
            for item in items:
                counts[item.status] = counts.get(item.status, 0) + 1
        else:
            counts = dict(db.execute(select(TaskItem.status, func.count()).where(TaskItem.task_id == task.id).group_by(TaskItem.status)).all())
    result = {k: getattr(task, k) for k in ('id', 'actor_id', 'owner_id', 'group_id', 'kind', 'status', 'cancelled', 'created_at', 'updated_at', 'finished_at')}
    template_candidates = items if details else list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id,
        TaskItem.operation == 'create', TaskItem.status.in_(['failed', 'cancelled']))))
    result['can_reprepare_templates'] = task.status not in ('running', 'queued') and any(
        i.operation == 'create' and i.status in ('failed', 'cancelled') and i.snapshot.get('upload_template_id')
        and not i.remote_write_attempted and i.stage in ('queued', 'validation', 'cancelled') for i in template_candidates)
    result.update(total=sum(counts.values()), counts=counts)
    if details:
        site_names = dict(db.execute(select(Site.id, Site.name).where(Site.id.in_({i.site_id for i in items}))).all())
        remote_ids = dict(db.execute(select(Distribution.id, Distribution.remote_id)
                                    .where(Distribution.id.in_({i.distribution_id for i in items if i.distribution_id}))).all())
        result['items'] = [{**{k: getattr(i, k) for k in ('id', 'channel_id', 'site_id', 'distribution_id', 'operation', 'status', 'stage', 'key_version', 'attempts', 'error', 'updated_at')},
                           'site_name': site_names.get(i.site_id, ''),
                           'remote_id': remote_ids.get(i.distribution_id)} for i in items]
    return api_json(result)


def refresh_task(db, task):
    statuses = list(db.scalars(select(TaskItem.status).where(TaskItem.task_id == task.id)))
    if not statuses:
        task.status = 'succeeded'
    elif any(s == 'running' for s in statuses):
        task.status = 'running'
    elif any(s == 'pending' for s in statuses):
        task.status = 'queued'
    elif any(s == 'needs_review' for s in statuses):
        task.status = 'needs_review'
    elif all(s == 'succeeded' for s in statuses):
        task.status = 'succeeded'
    elif all(s == 'cancelled' for s in statuses):
        task.status = 'cancelled'
    elif any(s == 'succeeded' for s in statuses):
        task.status = 'partial'
    else:
        task.status = 'failed'
    task.updated_at = utcnow()
    task.finished_at = None if task.status in ('queued', 'running') else utcnow()


def get_channel(db, user, channel_id, *, lock=False):
    query = select(Channel).where(Channel.id == channel_id)
    if lock:
        query = query.with_for_update()
    channel = db.scalar(query)
    if not channel:
        raise HTTPException(404, '渠道不存在')
    assert_owner(db, user, channel.owner_id)
    return channel


def get_task(db, user, task_id, *, lock=False):
    query = select(Task).where(Task.id == task_id)
    if lock:
        query = query.with_for_update()
    task = db.scalar(query)
    if not task:
        raise HTTPException(404, '任务不存在')
    assert_owner(db, user, task.owner_id)
    return task


def channel_snapshot(channel, fmt, site, *, partition_key=''):
    from .adapters.newapi_builds import build_snapshot
    from .credential_containers import partition_snapshot, primary_credential
    credential = (primary_credential(channel, fmt, partition_key, site=site)
                  if getattr(channel, 'key_mode', 'single') == 'multiple' or (fmt.schema_config or {}).get('type') in
                  ('aws_bedrock', 'vertex_gemini', 'vertex_claude', 'azure_gpt', 'azure_claude') else None)
    snap = {'models': list(channel.models), 'remark': channel.remark, 'declaration': channel.declaration,
            'format_code': fmt.code, 'format_version': fmt.version, 'category_id': channel.category_id,
            'channel_type': (fmt.schema_config or {}).get('remote_type', 1), 'format_schema': dict(fmt.schema_config or {}),
            'site_adapter': site.adapter, 'site_base_url': site.base_url, 'seller_user_id': str(site.seller_user_id),
            **build_snapshot(site),
            'routing_group': getattr(site, 'routing_group', None) or 'default', 'enable_strategy': 'disabled',
            **({'wire_format_schema': credential_wire_schema(credential, fmt.schema_config)}
               if (fmt.schema_config or {}).get('type') in ('aws_bedrock', 'vertex_gemini', 'vertex_claude') else {})}
    if (fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude'):
        from .azure_credentials import azure_credential_fields
        fields = azure_credential_fields(credential, fmt.schema_config['type'])
        snap.update(wire_format_schema=fields['credential_format'], credential_channel_config=fields['channel_config'],
                    channel_config=fields['channel_config'], config_explicit=True)
    snap.update(partition_snapshot(channel, fmt, partition_key, site=site))
    if snap.get('wire_format_schema', fmt.schema_config).get('type') == 'aws_api_key':
        from .adapters.bedrock_capabilities import bedrock_api_key_sdk_mode
        snap['bedrock_api_key_sdk'] = bedrock_api_key_sdk_mode(site.adapter, (site.capabilities or {}).get('verified_version'))
    return snap


def new_task(db, actor, owner_id, kind, **kwargs):
    task = Task(id=uid(), actor_id=actor.id, owner_id=owner_id, actor_session_version=actor.session_version,
                kind=kind, **kwargs)
    db.add(task)
    db.flush()
    return task


def check_no_pending(db, channel, *, allow_uncertain_tests=False, exclude_task_id=None):
    query = select(TaskItem.id).where(TaskItem.channel_id == channel.id,
                                     TaskItem.status.in_(['pending', 'running', 'needs_review']))
    if allow_uncertain_tests:
        query = query.where(~((TaskItem.operation == 'test') & (TaskItem.status == 'needs_review')))
    if exclude_task_id:
        query = query.where(TaskItem.task_id != exclude_task_id)
    pending = db.scalar(query.limit(1))
    if pending:
        raise HTTPException(409, '此渠道存在未完成或待核实任务，请先处理该任务')
