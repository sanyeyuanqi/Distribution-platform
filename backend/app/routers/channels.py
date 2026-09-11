# FastAPI intentionally declares dependency callables in defaults.
# ruff: noqa: B008
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from ..auth import (
    assert_owner,
    audit,
    get_current_user,
    redis_call,
    scope_owner_ids,
)
from ..channel_archive import archive_projection, verify_archive
from ..channel_service import (
    UploadInput,
    api_json,
    channel_snapshot,
    check_no_pending,
    get_channel,
    new_task,
    normalize_key,
    notify_worker,
    refresh_task,
    target_issues,
    task_json,
)
from ..channel_services import (
    channel_category_options,
    filter_service_variant,
    service_details,
)
from ..channel_statistics import distribution_usage, empty_usage, sum_distributions
from ..db import get_db, uid, utcnow
from ..distribution_monitoring import observation_json, record_observation
from ..distribution_status import observed_remote_state, upload_state
from ..models import Category, CredentialFormat, Site, SiteUploadTemplate, User
from ..models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from ..newapi_formats import (
    credential_fingerprint,
    credential_hint,
    credential_wire_schema,
    format_descriptor,
)
from ..remote_channel_names import new_remote_channel_name
from ..remote_channel_status import distribution_remote_state
from ..remote_usage_totals import manual_usage_evidence, remote_usage_total
from ..security import decrypt, encrypt, fingerprint

router = APIRouter(tags=['channels'])


def channel_context(db, channels, *, distributions=None, include_channel_tests=True):
    """Prefetch page associations and usage; query count does not grow per channel."""
    if not channels:
        return {'groups': {}, 'categories': {}, 'formats': {}, 'owners': {}, 'sites': {}, 'distributions': {}, 'usage': {}}
    if distributions is None:
        distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id.in_([c.id for c in channels]))))
    by_channel = {}
    for dist in distributions:
        by_channel.setdefault(dist.channel_id, []).append(dist)
    force_tasks = {}
    for channel_id, task_id, status in db.execute(select(TaskItem.channel_id, Task.id, Task.status)
            .join(Task, Task.id == TaskItem.task_id)
            .where(TaskItem.channel_id.in_([c.id for c in channels]), Task.kind == 'force_delete')
            .order_by(Task.created_at.desc())):
        force_tasks.setdefault(channel_id, {'id': task_id, 'status': status})
    context = {'groups': {r.id: r for r in db.scalars(select(UploadGroup).where(UploadGroup.id.in_({c.group_id for c in channels})))},
            'categories': {r.id: r for r in db.scalars(select(Category).where(Category.id.in_({c.category_id for c in channels})))},
            'formats': {r.id: r for r in db.scalars(select(CredentialFormat).where(CredentialFormat.id.in_({c.format_id for c in channels})))},
            'owners': {r.id: r for r in db.scalars(select(User).where(User.id.in_({c.owner_id for c in channels})))},
            'sites': {r.id: r for r in db.scalars(select(Site).where(Site.id.in_({d.site_id for d in distributions})))},
            'distributions': by_channel, 'usage': distribution_usage(db, channels), 'force_delete_tasks': force_tasks}
    from ..channel_reupload import reupload_projection
    from ..local_test_tasks import local_test_projection
    context['reupload'] = reupload_projection(db, channels, distributions, context['sites'], context['categories'], context['formats'])
    context['local_test'] = local_test_projection(db, channels, distributions)
    if include_channel_tests:
        from ..channel_local_tests import channel_test_projection
        context['channel_local_test'] = channel_test_projection(db, channels, distributions, context['local_test'])
    context['manual_usage_evidence'] = manual_usage_evidence(db, distributions)
    return context


def distribution_json(db, dist, context=None):
    from ..adapters.channel_observation import site_usage_conversion
    from ..distribution_tombstones import local_deletion
    if context is None:
        context = channel_context(db, [db.get(Channel, dist.channel_id)])
    deletion = local_deletion(dist)
    unverified_config = (dist.remote_snapshot or {}).get('_unverified_config_fields')
    site = context['sites'].get(dist.site_id) if context else db.get(Site, dist.site_id)
    usage = context['usage'].get(dist.id, empty_usage()) if context else distribution_usage(db, [db.get(Channel, dist.channel_id)]).get(dist.id, empty_usage())
    usage_status = ('verified_sources' if usage['has_verified_amount'] and not usage['unverified_count'] else 'pending_verification') if usage['fact_count'] else dist.usage_status
    return api_json({**{k: getattr(dist, k) for k in ('id', 'channel_id', 'site_id', 'remote_id', 'key_version', 'status',
                'partition_key', 'partition_label', 'key_count',
                'remote_name', 'models', 'routing_group', 'test_status', 'tested_at', 'usage_status', 'last_sync_at', 'error')},
            **distribution_remote_state(dist),
            **observed_remote_state(dist),
            **upload_state(dist, []),
            **context.get('local_test', {}).get(dist.id, {'local_test_available': False,
                'local_test_reason': '请刷新渠道状态后重新检查已上传 Key'}),
            'site_name': site.name if site else '', 'site_enabled': site.enabled if site else False,
            **context.get('reupload', {}).get(dist.id, {'reupload_available': False,
                'reupload_reason': '请刷新渠道状态后重新检查', 'reupload_task': None}),
            'locally_deleted': deletion is not None, 'deleted_at': deletion.get('at') if deletion else None,
            'remote_deletion_confirmed': False if deletion else None,
            'unverified_config_fields': ['base_url'] if isinstance(unverified_config, list)
                and 'base_url' in unverified_config else [],
            'account_info': dist.template_snapshot.get('effective_channel_config', {}).get('account_info', {}),
            'rpm_enabled': dist.template_snapshot.get('effective_channel_config', {}).get('rpm_enabled', False),
            'rpm_limit': dist.template_snapshot.get('effective_channel_config', {}).get('rpm_limit'),
            **{k: v for k, v in usage.items() if k != 'has_verified_amount'},
            'usage_status': usage_status, 'collection_status': dist.usage_status,
            'connectivity_test': observation_json(dist, 'test'),
            'usage_sync': observation_json(dist, 'sync_usage', conversion=site_usage_conversion(site)),
            'usage': usage['usage_by_unit'] or None, 'adopted_at': dist.adopted_at})


def channel_json(db, channel, context=None):
    context = context or channel_context(db, [channel])
    group, category, fmt = context['groups'][channel.group_id], context['categories'][channel.category_id], context['formats'][channel.format_id]
    distributions = context['distributions'].get(channel.id, [])
    owner = context['owners'][channel.owner_id]
    return api_json({**{k: getattr(channel, k) for k in ('id', 'display_id', 'owner_id', 'group_id', 'category_id', 'format_id', 'key_hint',
                     'key_version', 'key_mode', 'key_count', 'models', 'remark', 'declaration', 'upload_mode', 'archived', 'created_at')},
            'owner_name': owner.nickname or owner.username, 'owner_username': owner.username, 'group_tag': group.tag, 'group_name': group.name,
            'category_name': category.name, 'format_name': fmt.name, 'format_version': fmt.version,
            **service_details(category, fmt, channel.models),
            'credential_format': format_descriptor(fmt),
            'force_delete_task': context.get('force_delete_tasks', {}).get(channel.id),
            **archive_projection(channel, distributions),
            **context.get('channel_local_test', {}).get(channel.id, {}),
            'proxy_hint': '已配置（加密保存）' if channel.proxy_encrypted else '',
            'distribution_count': len(distributions), 'remote_count': sum(d.remote_id is not None for d in distributions),
            'remote_channels': sum(d.remote_id is not None for d in distributions),
            'distributions': [distribution_json(db, d, context) for d in distributions],
            'remote_usage_total': remote_usage_total(distributions, context['sites'],
                                                    evidence=context.get('manual_usage_evidence', {})),
            **sum_distributions(distributions, context['usage'])})


@router.get('/channels')
def channels(owner_id: str | None = None, category_id: str | None = None,
             group_id: str | None = None, search: str = '', site_id: str | None = None,
             model: str | None = None, status: str | None = None, archived: bool | Literal['all'] = False,
             variant: str | None = None,
             offset: int = 0, limit: int = 100, user=Depends(get_current_user), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user, owner_id=owner_id)
    query = select(Channel).where(Channel.owner_id.in_(owners))
    if archived != 'all':
        query = query.where(Channel.archived.is_(archived))
    if category_id:
        query = query.where(Channel.category_id == category_id)
    query = filter_service_variant(db, query, variant)
    if group_id:
        query = query.where(Channel.group_id == group_id)
    if search:
        term = '%' + search[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.join(User, User.id == Channel.owner_id).join(UploadGroup, UploadGroup.id == Channel.group_id)
        query = query.where(Channel.remark.ilike(term, escape='\\') | Channel.key_hint.ilike(term, escape='\\')
            | cast(Channel.display_id, String).ilike(term, escape='\\')
            | Channel.id.ilike(term, escape='\\') | User.username.ilike(term, escape='\\')
            | User.nickname.ilike(term, escape='\\') | UploadGroup.tag.ilike(term, escape='\\')
            | UploadGroup.name.ilike(term, escape='\\'))
    if site_id or status:
        sub = select(Distribution.channel_id)
        if site_id:
            sub = sub.where(Distribution.site_id == site_id)
        if status:
            sub = sub.where(Distribution.status == status)
        query = query.where(Channel.id.in_(sub))
    start, count = max(0, offset), min(500, max(1, limit))
    if model:
        # Portable JSON-list membership for both SQLite tests and PostgreSQL.
        rows = list(db.scalars(query.order_by(Channel.created_at.desc(), Channel.id.desc())))
        rows = [r for r in rows if model in r.models]
        total, rows = len(rows), rows[start:start + count]
    else:
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = list(db.scalars(query.order_by(Channel.created_at.desc(), Channel.id.desc()).offset(start).limit(count)))
    context = channel_context(db, rows)
    return {'items': [channel_json(db, row, context) for row in rows], 'total': total}


@router.get('/channel-uploaders')
def channel_uploaders(user=Depends(get_current_user), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user)
    # Historical channel owners stay available across all list filters.
    uploaded = select(Channel.owner_id).where(Channel.owner_id.in_(owners))
    items = [dict(row) for row in db.execute(select(User.id, User.username, User.nickname)
        .where(User.id.in_(uploaded)).order_by(User.display_id, User.id)).mappings()]
    return {'items': items, 'total': len(items)}


@router.get('/channel-categories')
def channel_categories(owner_id: str | None = None, archived: bool | Literal['all'] = False,
                       user=Depends(get_current_user), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user, owner_id=owner_id)
    items = channel_category_options(db, owners, archived)
    return {'items': items, 'total': len(items)}


@router.get('/channel-distribution-sites')
def channel_distribution_sites(owner_id: str | None = None,
                               user=Depends(get_current_user), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user, owner_id=owner_id)
    # Historical destinations remain selectable even when the site or its
    # channels are archived, or the current list filters have no matches.
    authorized_sites = select(Distribution.site_id).join(Channel, Channel.id == Distribution.channel_id).where(
        Channel.owner_id.in_(owners))
    items = [dict(row) for row in db.execute(select(
        Site.id, Site.display_id, Site.name, Site.enabled, Site.archived)
        .where(Site.id.in_(authorized_sites)).order_by(Site.display_id, Site.id)).mappings()]
    return {'items': items, 'total': len(items)}


@router.get('/channel-distributions')
def channel_distributions(owner_id: str | None = None, category_id: str | None = None,
                          variant: str | None = None, archived: bool | Literal['all'] = False, search: str = '',
                          site_id: str | None = None,
                          offset: int = 0, limit: int = 50,
                          user=Depends(get_current_user), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user, owner_id=owner_id)
    channel_query = select(Channel).where(Channel.owner_id.in_(owners))
    if archived != 'all':
        channel_query = channel_query.where(Channel.archived.is_(archived))
    if category_id:
        channel_query = channel_query.where(Channel.category_id == category_id)
    channel_query = filter_service_variant(db, channel_query, variant)
    query = select(Distribution).join(Channel, Channel.id == Distribution.channel_id).where(
        Channel.id.in_(channel_query.with_only_columns(Channel.id)))
    if site_id:
        query = query.where(Distribution.site_id == site_id)
    if search:
        term = '%' + search[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        query = query.join(UploadGroup, UploadGroup.id == Channel.group_id).join(Site, Site.id == Distribution.site_id)
        query = query.join(User, User.id == Channel.owner_id)
        query = query.where(Channel.id.ilike(term, escape='\\') | Channel.remark.ilike(term, escape='\\')
            | cast(Channel.display_id, String).ilike(term, escape='\\')
            | UploadGroup.tag.ilike(term, escape='\\') | UploadGroup.name.ilike(term, escape='\\')
            | Site.name.ilike(term, escape='\\') | Distribution.remote_name.ilike(term, escape='\\')
            | Distribution.remote_id.ilike(term, escape='\\') | User.username.ilike(term, escape='\\')
            | User.nickname.ilike(term, escape='\\'))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    distributions = list(db.scalars(query.order_by(Distribution.created_at.desc(), Distribution.id.desc())
        .offset(max(0, offset)).limit(min(500, max(1, limit)))))
    channels = list(db.scalars(select(Channel).options(
        defer(Channel.key_encrypted, raiseload=True), defer(Channel.fingerprint, raiseload=True))
        .where(Channel.id.in_({dist.channel_id for dist in distributions}))))
    context = channel_context(db, channels, distributions=distributions, include_channel_tests=False)
    summaries = {}
    for channel in channels:
        group, owner = context['groups'][channel.group_id], context['owners'][channel.owner_id]
        service = service_details(context['categories'][channel.category_id], context['formats'][channel.format_id], channel.models)
        summaries[channel.id] = {**{key: getattr(channel, key) for key in (
            'id', 'display_id', 'group_id', 'remark', 'category_id', 'owner_id', 'archived')},
            'group_tag': group.tag, 'group_name': group.name, **service,
            'owner_name': owner.nickname or owner.username, 'owner_username': owner.username}
    return api_json({'items': [{**distribution_json(db, dist, context), 'channel': summaries[dist.channel_id],
        'remote_usage_total': remote_usage_total([dist], context['sites'],
            evidence=context.get('manual_usage_evidence', {}))} for dist in distributions], 'total': total})


@router.get('/channels/action-tasks')
def cleanup_tasks(kind: Literal['force_delete_async'] = 'force_delete_async', offset: int = 0, limit: int = 100,
                  user=Depends(get_current_user), db: Session = Depends(get_db)):
    from ..remote_cleanup import cleanup_json
    owners = scope_owner_ids(db, user, 'team')
    query = select(Task).where(Task.kind == kind, Task.owner_id.in_(owners))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(db.scalars(query.order_by(Task.created_at.desc(), Task.id.desc())
                          .offset(max(0, offset)).limit(min(100, max(1, limit)))))
    return {'items': [cleanup_json(db, task) for task in rows], 'total': total}


@router.get('/channels/{channel_id}')
def detail(channel_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    channel = get_channel(db, user, channel_id)
    result = channel_json(db, channel)
    result['versions'] = [{'version': v.version, 'valid_from': v.valid_from, 'valid_to': v.valid_to}
                          for v in db.scalars(select(KeyVersion).where(KeyVersion.channel_id == channel.id).order_by(KeyVersion.version.desc()))]
    return api_json(result)


@router.post('/channels/{channel_id}/reveal')
def reveal(channel_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    channel = get_channel(db, user, channel_id, lock=True)
    counter = f'key-reveal:{user.id}:{utcnow().strftime("%Y%m%d%H%M")}'
    amount = redis_call('incr', counter)
    if amount == 1:
        redis_call('expire', counter, 90)
    if amount > 30:
        raise HTTPException(429, '完整密钥查看过于频繁，请稍后重试')
    audit(db, user, 'channel.reveal', 'channel', channel.id)
    db.commit()
    from ..credential_containers import upload_text
    return {'key': upload_text(channel)}


class PatchChannel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    remark: str | None = Field(default=None, max_length=4000)
    declaration: str | None = Field(default=None, max_length=4000)
    models: list[str] | None = Field(default=None, max_length=200)

    @field_validator('models')
    @classmethod
    def validate_models(cls, models):
        if models is not None and not models:
            raise ValueError('模型不能为空')
        return UploadInput.model_list(models) if models is not None else None


def existing_operation(db, actor, channel, operation, distributions, changes=None, *, model=None, test_all=False, test_content=None):
    fmt = db.get(CredentialFormat, channel.format_id)
    task = new_task(db, actor, channel.owner_id, operation, group_id=channel.group_id)
    for dist in distributions:
        site = db.get(Site, dist.site_id)
        if operation == 'test':
            from ..local_test_tasks import (
                confirmed_uploaded_version,
                local_test_snapshot,
            )
            uploaded_version = confirmed_uploaded_version(db, channel, dist)
            snap = local_test_snapshot(channel, fmt, dist, site, db.get(Category, channel.category_id), model,
                                       uploaded_version=uploaded_version, test_all=test_all, test_content=test_content)
        else:
            snap = channel_snapshot(channel, fmt, site, partition_key=dist.partition_key)
        if 'bedrock_api_key_sdk' in snap:
            snap['bedrock_api_key_sdk'] = bool(dist.template_snapshot.get('bedrock_api_key_sdk', False))
        snap.update(models=list(dist.models), routing_group=dist.routing_group,
                    remark=dist.template_snapshot.get('effective_remark', channel.remark))
        snap.update(changes=changes or {}, expected=dict(dist.remote_snapshot or {}))
        if operation == 'delete_remote':
            snap['delete_target'] = {'id': str(dist.remote_id), 'name': dist.remote_name,
                                     'type': snap['channel_type']}
        if operation in ('test', 'sync_usage'):
            snap['operation_target'] = {'id': str(dist.remote_id), 'name': dist.remote_name,
                                        'type': snap['channel_type']}
            if operation == 'test':
                snap['test_model'] = model
        item = TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                        operation=operation, key_version=channel.key_version, snapshot=snap)
        db.add(item)
        record_observation(dist, item, status='pending', replace=True)
    db.flush()
    return task


@router.patch('/channels/{channel_id}')
def patch(channel_id: str, payload: PatchChannel, user=Depends(get_current_user), db: Session = Depends(get_db)):
    channel = get_channel(db, user, channel_id, lock=True)
    if channel.archived:
        raise HTTPException(409, '已归档渠道不能修改')
    if channel.upload_mode == 'template' and payload.models is not None and payload.models != channel.models:
        raise HTTPException(422, '模板渠道的模型由各站点模板分别配置，不能整体覆盖；请联系管理员维护站点模板')
    if payload.models is not None:
        from ..google_services import service_model_issues
        fmt = db.get(CredentialFormat, channel.format_id)
        if issues := service_model_issues(fmt.schema_config, payload.models):
            raise HTTPException(422, '；'.join(issues))
    check_no_pending(db, channel)
    changes = {}
    for key in ('remark', 'declaration', 'models'):
        value = getattr(payload, key)
        if value is not None and value != getattr(channel, key):
            setattr(channel, key, value)
            if key != 'declaration':
                changes[key] = ','.join(value) if key == 'models' else value
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id,
                                  Distribution.remote_id.is_not(None), Distribution.status != 'deleted')))
    task = existing_operation(db, user, channel, 'edit', distributions, changes) if changes and distributions else None
    audit(db, user, 'channel.edit', 'channel', channel.id, {'fields': list(payload.model_fields_set)})
    db.commit()
    if task:
        notify_worker()
    return {**channel_json(db, channel), 'task_id': task.id if task else None}


class Rotate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=200000)


@router.post('/channels/{channel_id}/rotate')
def rotate(channel_id: str, payload: Rotate, user=Depends(get_current_user), db: Session = Depends(get_db)):
    selected = get_channel(db, user, channel_id)
    db.scalar(select(User).where(User.id == selected.owner_id).with_for_update(key_share=True))
    channel = get_channel(db, user, channel_id, lock=True)
    if channel.archived:
        raise HTTPException(409, '已归档渠道不能更换密钥')
    check_no_pending(db, channel)
    fmt = db.get(CredentialFormat, channel.format_id)
    from ..credential_containers import (
        bundle_hint,
        bundle_identity,
        channel_entries,
        channel_partitions,
        encode_entries,
        partition_entries,
        replace_member_index,
    )
    try:
        if channel.key_mode == 'multiple':
            from ..channel_service import parse_rows
            rows, errors = parse_rows(payload.key, fmt=fmt, limit=channel.key_count)
            if errors or len(rows) != channel.key_count or any(row['status'] != 'valid' for row in rows):
                raise ValueError(f'请完整提供 {channel.key_count} 条不重复密钥，保持原顺序和认证/资源配置')
            entries = [{**old, 'key': row['key']} for old, row in zip(channel_entries(channel), rows)]
            old_parts, new_parts = channel_partitions(channel, fmt), partition_entries(entries, fmt)
            if [(p['partition_key'], p['indices']) for p in old_parts] != [(p['partition_key'], p['indices']) for p in new_parts]:
                raise ValueError('整组更换必须保持原顺序及认证方式、Azure资源与版本；不能改变远端分区')
            key = encode_entries(entries)
        else:
            entries = [{'key': normalize_key(payload.key, fmt), 'remark': channel.remark,
                        'proxy': decrypt(channel.proxy_encrypted) if channel.proxy_encrypted else ''}]
            key = entries[0]['key']
        if channel.key_mode == 'single' and (fmt.schema_config or {}).get('type') == 'aws_bedrock' and credential_wire_schema(
                key, fmt.schema_config) != credential_wire_schema(decrypt(channel.key_encrypted), fmt.schema_config):
            raise ValueError('更换密钥须保持原渠道的 AK/SK 或 API Key 类型；切换类型请重新上传')
        if channel.key_mode == 'single' and (fmt.schema_config or {}).get('type') in ('vertex_gemini', 'vertex_claude') and credential_wire_schema(
                key, fmt.schema_config) != credential_wire_schema(decrypt(channel.key_encrypted), fmt.schema_config):
            raise ValueError('更换密钥须保持原渠道的服务账号 JSON 或 API Key 认证方式；切换方式请重新上传')
        if channel.key_mode == 'single' and (fmt.schema_config or {}).get('type') in ('azure_gpt', 'azure_claude'):
            from ..azure_credentials import azure_credential_fields
            kind = fmt.schema_config['type']
            if (azure_credential_fields(key, kind)['channel_config']
                    != azure_credential_fields(decrypt(channel.key_encrypted), kind)['channel_config']):
                raise ValueError('更换 Azure 密钥须保持原资源和 API 版本；更换资源请重新上传')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    identity = bundle_identity(entries, fmt) if channel.key_mode == 'multiple' else credential_fingerprint(key, fmt)
    if identity == channel.fingerprint:
        return {**channel_json(db, channel), 'task_id': None}
    from ..channel_service import existing_key_channels
    if any(row.id != channel.id for entry in entries for row in existing_key_channels(db, channel.owner_id, entry['key'], fmt,
            db.get(Category, channel.category_id), models=channel.models)):
        raise HTTPException(409, '本人的其他渠道已使用该密钥')
    old = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id, KeyVersion.version == channel.key_version))
    if old:
        old.valid_to = utcnow()
    channel.key_version += 1
    channel.key_encrypted, channel.fingerprint = encrypt(key), identity
    channel.key_hint = bundle_hint(entries, fmt) if channel.key_mode == 'multiple' else credential_hint(key, fmt)
    replace_member_index(db, channel, entries, fmt)
    db.add(KeyVersion(channel_id=channel.id, version=channel.key_version, key_encrypted=channel.key_encrypted, fingerprint=channel.fingerprint))
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id,
                                  Distribution.remote_id.is_not(None), Distribution.status != 'deleted')))
    task = existing_operation(db, user, channel, 'rotate', distributions) if distributions else None
    audit(db, user, 'channel.rotate', 'channel', channel.id, {'version': channel.key_version, 'target_count': len(distributions)})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '密钥更换与其他请求冲突，请刷新') from None
    if task:
        notify_worker()
    return {**channel_json(db, channel), 'task_id': task.id if task else None}


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['enable', 'disable', 'test', 'redistribute', 'reupload', 'archive', 'unarchive', 'delete_remote', 'force_delete_local', 'force_delete', 'force_delete_async']
    site_ids: list[str] | None = None
    distribution_ids: list[str] | None = Field(default=None, min_length=1, max_length=500)
    confirmation: str | None = None
    model: str | None = Field(default=None, max_length=200)
    test_all: bool = Field(default=False, strict=True)
    test_scope: Literal['distribution', 'channel'] = 'distribution'
    test_content: str | None = Field(default=None, strict=True, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)

    @field_validator('model')
    @classmethod
    def validate_model(cls, model):
        return UploadInput.model_list([model])[0] if model is not None else None

    @field_validator('test_content')
    @classmethod
    def validate_test_content(cls, content):
        if content is None or not content.strip():
            raise ValueError('测试内容不能为空')
        if any(ord(char) < 32 and char not in '\n\r\t' or ord(char) == 127 for char in content):
            raise ValueError('测试内容不能含无效控制字符')
        try:
            content.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('测试内容包含无效字符') from None
        return content.strip()

    @model_validator(mode='after')
    def validate_test_options(self):
        if self.action != 'test' and (self.test_all or self.test_content is not None or self.test_scope != 'distribution'):
            raise ValueError('只有连通性测试可以设置模型范围和测试内容')
        if self.test_all and self.model is not None:
            raise ValueError('测试全部模型时不能同时指定单个模型')
        if self.test_scope == 'channel' and (self.site_ids is not None or self.distribution_ids is not None):
            raise ValueError('渠道模型并集测试不能同时指定单个站点或分发')
        return self


def action_request_values(payload):
    # Keep old omitted defaults byte-compatible with already submitted request
    # hashes, including force-delete replay after the local channel is gone.
    values = payload.model_dump(exclude={'idempotency_key'})
    if not payload.test_all:
        values.pop('test_all')
    if payload.test_content is None:
        values.pop('test_content')
    if payload.test_scope == 'distribution':
        values.pop('test_scope')
    return values


@router.post('/channels/{channel_id}/actions')
def actions(channel_id: str, payload: Action, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.action == 'force_delete_local':
        raise HTTPException(422, '强制删除现会同时删除远端与本地记录，请刷新页面后重新确认')
    if payload.action == 'force_delete_async' and payload.idempotency_key:
        from ..remote_cleanup import lock_request
        lock_request(db, user.id, payload.idempotency_key)
    if payload.action in ('force_delete', 'force_delete_async') and payload.idempotency_key:
        previous = db.scalar(select(Task).where(Task.actor_id == user.id, Task.idempotency_key == payload.idempotency_key))
        if previous:
            assert_owner(db, user, previous.owner_id)
            if previous.kind != payload.action or previous.request_hash != force_request_hash(channel_id, payload):
                raise HTTPException(409, '该请求标识已用于不同操作，请刷新后重新确认')
            if previous.kind == 'force_delete_async':
                from ..remote_cleanup import cleanup_json
                return cleanup_json(db, previous)
            return force_task_json(db, previous)
    selected = get_channel(db, user, channel_id)
    db.scalar(select(User).where(User.id == selected.owner_id).with_for_update(key_share=True))
    channel = get_channel(db, user, channel_id, lock=True)
    if payload.action == 'force_delete_async':
        from ..remote_cleanup import enqueue_cleanup
        result = enqueue_cleanup(db, user, channel, payload, force_request_hash(channel_id, payload))
        notify_worker()
        return result
    if payload.action == 'reupload':
        from ..channel_reupload import reupload
        task = reupload(db, user, channel, payload)
        notify_worker()
        return task_json(db, task, details=True)
    if payload.action == 'force_delete':
        return force_delete(db, user, channel, payload)
    request_hash = None
    if payload.idempotency_key is not None:
        if payload.action != 'test':
            raise HTTPException(422, '只有连通性测试可以使用此请求标识')
        request_values = action_request_values(payload)
        if payload.distribution_ids is None:
            request_values.pop('distribution_ids')
        request_hash = fingerprint(json.dumps({'channel_id': channel.id,
            **request_values}, sort_keys=True, ensure_ascii=False))
        previous = db.scalar(select(Task).where(Task.actor_id == user.id, Task.idempotency_key == payload.idempotency_key))
        if previous:
            if previous.request_hash != request_hash or previous.owner_id != channel.owner_id:
                raise HTTPException(409, '该请求标识已用于不同的渠道测试')
            return task_json(db, previous, details=True)
    check_no_pending(db, channel, allow_uncertain_tests=payload.action == 'test')
    if payload.action == 'unarchive':
        if payload.site_ids is not None or payload.distribution_ids is not None:
            raise HTTPException(422, '取消归档针对整个本地渠道，不能只选择部分远端分发')
        if payload.model is not None:
            raise HTTPException(422, '只有连通性测试需要指定模型')
        if channel.archived:
            channel.archived = False
            audit(db, user, 'channel.unarchive', 'channel', channel.id)
            db.commit()
        return channel_json(db, channel)
    if payload.action == 'archive':
        if payload.site_ids is not None or payload.distribution_ids is not None:
            raise HTTPException(422, '归档针对整个本地渠道，不能只选择部分远端分发')
        checks = verify_archive(db, channel)
        channel.archived = True
        audit(db, user, 'channel.archive', 'channel', channel.id, {'remote_status_checks': checks})
        db.commit()
        return channel_json(db, channel)
    if channel.archived:
        raise HTTPException(409, '渠道已归档')
    if payload.site_ids is not None and payload.distribution_ids is not None:
        raise HTTPException(422, '请选择站点范围或具体分发记录，不能同时提供')
    if (payload.action == 'test' and payload.test_scope != 'channel'
            and payload.distribution_ids is None and (payload.site_ids is None or len(payload.site_ids) != 1)):
        raise HTTPException(422, '请选择一个具体的站点分发')
    if payload.action != 'test' and payload.model is not None:
        raise HTTPException(422, '只有连通性测试需要指定模型')
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id)))
    fmt = db.get(CredentialFormat, channel.format_id)
    if payload.action == 'test' and payload.test_scope == 'channel':
        from ..channel_local_tests import enqueue_channel_test
        task = enqueue_channel_test(db, user, channel, distributions, model=payload.model,
                                    test_all=payload.test_all, test_content=payload.test_content)
    elif payload.action == 'redistribute':
        if payload.distribution_ids is not None:
            raise HTTPException(422, '补分发请选择目标站点，现有分发记录请从原任务重试')
        if channel.upload_mode == 'template':
            from ..upload_templates import SimpleUploadInput, prepare_simple_upload
            from .uploads import add_template_distribution
            # Dedup is evaluated against the immutable owner, even when their manager acts.
            owner = db.get(User, channel.owner_id)
            from ..credential_containers import channel_entries, upload_text
            entries = channel_entries(channel)
            result, rows, template_targets, _, template_fmt = prepare_simple_upload(db, owner, SimpleUploadInput(
                category_id=channel.category_id, format_id=channel.format_id, upload_mode='batch' if channel.key_mode == 'multiple' else 'single',
                credentials=upload_text(channel), remarks='\n'.join(row['remark'] for row in entries),
                proxies='\n'.join(row['proxy'] for row in entries),
                **(channel.upload_settings or {})), legacy_channel=channel)
            if not result['can_submit']:
                raise HTTPException(422, {'message': '模板补分发校验未通过', 'preview': result})
            missing = set(rows[0]['missing_site_ids'])
            if payload.site_ids is not None:
                if set(payload.site_ids) - missing:
                    raise HTTPException(422, '目标必须是尚未分发且已启用此分类模板的站点')
                missing &= set(payload.site_ids)
            if not missing:
                raise HTTPException(422, '没有需要补分发的模板站点')
            task = new_task(db, user, channel.owner_id, 'redistribute', group_id=channel.group_id,
                            snapshot={'upload_mode': 'template', 'site_ids': sorted(missing)})
            for template, site in template_targets:
                if site.id in missing:
                    add_template_distribution(db, task, channel, template_fmt, template, site)
            db.flush()
            refresh_task(db, task)
            audit(db, user, 'channel.redistribute', 'channel', channel.id, {'task_id': task.id, 'upload_mode': 'template'})
            db.commit()
            notify_worker()
            return task_json(db, task, details=True)
        if db.scalar(select(SiteUploadTemplate.id).where(SiteUploadTemplate.category_id == channel.category_id).limit(1)):
            raise HTTPException(422, '此分类已由站点模板管理，旧高级渠道不能绕过模板补分发；请联系管理员核对现有配置')
        sites = list(db.scalars(select(Site).where(Site.enabled.is_(True), Site.archived.is_(False))))
        sites = [s for s in sites if not any(d.site_id == s.id for d in distributions)]
        if payload.site_ids is not None:
            allowed = {s.id for s in sites}
            if set(payload.site_ids) - allowed:
                raise HTTPException(422, '包含已分发、未启用或不存在的站点；失败项请从原任务重试')
            sites = [s for s in sites if s.id in payload.site_ids]
        if not sites:
            raise HTTPException(422, '没有需要补分发的目标站点')
        task = new_task(db, user, channel.owner_id, 'redistribute', group_id=channel.group_id,
                        snapshot={'site_ids': [s.id for s in sites]})
        for site in sites:
            issues = target_issues(site, fmt, channel.models, proxy=bool(channel.proxy_encrypted))
            dist = Distribution(id=uid(), channel_id=channel.id, site_id=site.id, key_version=channel.key_version,
                                models=channel.models, remote_name=new_remote_channel_name(db, site, channel))
            db.add(dist)
            db.flush()
            db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                            operation='create', key_version=channel.key_version, snapshot=channel_snapshot(channel, fmt, site),
                            status='failed' if issues else 'pending', stage='validation' if issues else 'queued',
                            error='；'.join(issues) if issues else None))
    else:
        targets = [d for d in distributions if payload.action == 'test' or (d.remote_id and d.status != 'deleted')]
        if payload.site_ids is not None:
            if set(payload.site_ids) - {d.site_id for d in targets}:
                raise HTTPException(422, '目标不属于此渠道的现有远端分发')
            targets = [d for d in targets if d.site_id in payload.site_ids]
        if payload.distribution_ids is not None:
            if set(payload.distribution_ids) - {d.id for d in targets}:
                raise HTTPException(422, '目标不属于此渠道的现有远端分发')
            targets = [d for d in targets if d.id in payload.distribution_ids]
        if not targets:
            raise HTTPException(422, '没有可执行的目标分发')
        if payload.action == 'test' and len(targets) != 1:
            raise HTTPException(422, '此站点包含多个远端渠道，请选择一条具体分发记录')
        if payload.action == 'test' and not payload.test_all and (not payload.model or payload.model not in targets[0].models):
            raise HTTPException(422, '请选择此站点分发已配置的模型')
        if payload.action == 'delete_remote' and payload.confirmation != f'DELETE {len(targets)}':
            raise HTTPException(422, f'删除需要明确确认 DELETE {len(targets)}；将删除所选远端渠道，历史消耗与结算保留')
        task = existing_operation(db, user, channel, payload.action, targets, model=payload.model,
                                  test_all=payload.test_all, test_content=payload.test_content)
    if request_hash:
        task.idempotency_key, task.request_hash = payload.idempotency_key, request_hash
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            previous = db.scalar(select(Task).where(Task.actor_id == user.id, Task.idempotency_key == payload.idempotency_key))
            if previous and previous.request_hash == request_hash and previous.owner_id == channel.owner_id:
                return task_json(db, previous, details=True)
            raise HTTPException(409, '该请求标识已被另一渠道测试使用，请刷新后重试') from None
    else:
        db.flush()
    refresh_task(db, task)
    audit(db, user, 'channel.' + payload.action, 'channel', channel.id, {'task_id': task.id})
    db.commit()
    notify_worker()
    return task_json(db, task, details=True)


def force_request_hash(channel_id, payload):
    return fingerprint(json.dumps({'channel_id': channel_id,
        **action_request_values(payload)}, sort_keys=True, ensure_ascii=False))


def force_task_json(db, task):
    result = task_json(db, task, details=True)
    completed = [item.snapshot.get('operation_result', {}) for item in db.scalars(
        select(TaskItem).where(TaskItem.task_id == task.id, TaskItem.operation == 'force_delete'))]
    result['result'] = {
        'deleted_distribution_count': sum(row.get('deleted_distribution_count', 0) for row in completed),
        'deleted_channel_count': sum(row.get('deleted_channel_count', 0) for row in completed),
        'remote_confirmed': any(row.get('remote_confirmed') is True for row in completed),
    }
    return result


@router.get('/channels/action-tasks/{task_id}')
def action_task_status(task_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or task.kind not in ('force_delete', 'force_delete_async'):
        raise HTTPException(404, '删除任务不存在')
    assert_owner(db, user, task.owner_id)
    if task.kind == 'force_delete_async':
        from ..remote_cleanup import cleanup_json
        return cleanup_json(db, task)
    return force_task_json(db, task)


@router.post('/channels/action-tasks/{task_id}/retry')
def retry_cleanup_task(task_id: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    from ..remote_cleanup import retry_cleanup
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if not task or task.kind != 'force_delete_async':
        raise HTTPException(404, '远端清理任务不存在')
    result = retry_cleanup(db, user, task)
    notify_worker()
    return result


def force_delete(db, user, channel, payload):
    from ..channel_purge import purge_distributions
    from ..distribution_tombstones import locally_deleted

    ids = payload.distribution_ids
    if (not ids or len(set(ids)) != len(ids) or payload.site_ids is not None
            or payload.model is not None):
        raise HTTPException(422, '强制删除须提供不重复的具体分发记录，不能使用站点或模型范围')
    if payload.confirmation != f'DELETE REMOTE AND LOCAL {len(ids)}':
        raise HTTPException(422, '请重新确认同时删除所选远端渠道与本地记录')
    checked = purge_distributions(db, user, channel, ids, validate_only=True)
    old_items, targets = checked['items'], checked['targets']
    task = new_task(db, user, channel.owner_id, 'force_delete', group_id=channel.group_id)
    task.idempotency_key = payload.idempotency_key
    task.request_hash = force_request_hash(channel.id, payload)
    fmt = db.get(CredentialFormat, channel.format_id)
    for dist in targets:
        site = db.get(Site, dist.site_id)
        related = [item for item in old_items if item.distribution_id == dist.id]
        channel_type = (dist.remote_snapshot or {}).get('type') or (dist.template_snapshot or {}).get('channel_type')
        if type(channel_type) is not int:
            channel_type = (fmt.schema_config or {}).get('remote_type') if fmt else None
        if type(channel_type) is not int or not dist.remote_name:
            raise HTTPException(422, '分发缺少可核对的远端名称或类型，请先核实关联')
        from ..adapters.newapi_builds import build_snapshot
        snap = {'site_adapter': site.adapter, 'site_base_url': site.base_url, **build_snapshot(site),
            'seller_user_id': str(site.seller_user_id), 'partition_key': dist.partition_key,
            'delete_target': {'id': str(dist.remote_id) if dist.remote_id else None,
                              'name': dist.remote_name, 'type': channel_type},
            'remote_never_created': not dist.remote_id and not any(item.remote_write_attempted
                and not (item.operation == 'test' and (item.snapshot or {}).get('test_source') == 'local')
                for item in related)
                and not db.scalar(select(DistributionVersion.id).where(DistributionVersion.distribution_id == dist.id).limit(1))}
        acknowledged = dist.status == 'deleted' and not locally_deleted(dist) and any(item.operation in ('delete_remote', 'force_delete')
            and item.snapshot.get('delete_acknowledged') is True
            and item.snapshot.get('delete_target') == snap['delete_target']
            and all(item.snapshot.get(field) == snap[field] for field in ('site_adapter', 'site_base_url', 'seller_user_id'))
            for item in related)
        if acknowledged:
            snap['delete_acknowledged'] = True
        db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                        operation='force_delete', snapshot=snap,
                        stage='remote_deleted_pending_purge' if acknowledged else 'queued'))
    for item in old_items:
        if item.status in ('pending', 'failed', 'needs_review', 'running'):
            item.status, item.lease_until, item.next_attempt_at = 'cancelled', None, None
            record_observation(db.get(Distribution, item.distribution_id), item)
    for task_id in {item.task_id for item in old_items}:
        refresh_task(db, db.get(Task, task_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '删除请求已提交或记录已变化，请刷新后查看原任务') from None
    refresh_task(db, task)
    audit(db, user, 'channel.force_delete.requested', 'channel', channel.id, {'task_id': task.id, 'target_count': len(ids)})
    db.commit()
    notify_worker()
    return force_task_json(db, task)
