"""Explicitly prepare one failed distribution using current credentials/templates."""
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from .auth import assert_owner, audit
from .channel_service import new_task, refresh_task, supported_format, target_issues
from .credential_containers import partition_for
from .distribution_status import upload_state
from .distribution_tombstones import locally_deleted
from .models import Category, CredentialFormat, Site, SiteUploadTemplate
from .models_channels import Channel, Distribution, KeyVersion, Task, TaskItem
from .security import encrypt, fingerprint
from .site_verification import verification_state
from .upload_templates import (
    distribution_template_snapshot,
    effective_template,
    same_format_type,
    template_issues,
    template_variant,
)

BUSY = frozenset({'pending', 'running', 'needs_review'})


def eligibility_reason(channel, dist, items):
    if channel.archived:
        return '渠道已归档，不能重新上传'
    if locally_deleted(dist) or dist.status == 'deleted':
        return '分发已删除，不能重新上传'
    if dist.remote_id:
        return '此分发已有远端渠道，不能重新上传覆盖'
    if any(item.status in BUSY for item in items):
        return '此分发存在执行中或待核实任务，请先处理原任务'
    if dist.status != 'failed':
        return '只有创建失败且没有远端编号的分发可以重新上传'
    if not any(item.operation == 'create' and item.status == 'failed' for item in items):
        return '未找到此分发的创建失败记录'
    if any(item.operation == 'create' and item.status == 'succeeded' for item in items):
        return '此分发曾成功创建，请先核实远端关联'
    if not dist.remote_name:
        return '分发缺少稳定名称，请先核实关联'
    return None


def choose_template(channel, category, fmt, templates, formats):
    if not supported_format(category, fmt):
        return None, '渠道分类或凭据格式已停用或不可用'
    matching = [template for template in templates
                if same_format_type(fmt, formats.get(template.format_id))
                and template_variant(category, formats.get(template.format_id), template.models)
                == template_variant(category, fmt, channel.models)]
    if len(matching) != 1:
        return None, '此站点没有唯一匹配的分发模板，请联系管理员配置'
    template = matching[0]
    if not template.enabled:
        return None, '此站点的分发模板已停用，请联系管理员启用'
    if not supported_format(category, formats.get(template.format_id)):
        return None, '当前分发模板的凭据格式已停用或不可用'
    return template, None


def configuration_reason(channel, site, category, fmt, template):
    if not site or site.archived or not site.enabled:
        return '目标站点已停用或归档，请联系管理员处理'
    if verification_state(site)[0] != 'verified':
        return '目标站点尚未通过验证，请联系管理员验证'
    try:
        effective = effective_template(template, channel.upload_settings, channel.proxy_encrypted,
                                       category=category, fmt=fmt)
        issues = template_issues(template, site, category, fmt, effective=effective)
    except (ValueError, TypeError, AttributeError):
        return '当前分发模板配置不可用，请联系管理员检查'
    if issues:
        from .adapters.newapi_builds import GROUP_TOO_LONG_MESSAGE
        return GROUP_TOO_LONG_MESSAGE if GROUP_TOO_LONG_MESSAGE in issues else '当前分发模板配置不可用，请联系管理员检查'
    return None


def reupload_projection(db, channels, distributions, sites, categories, formats):
    """Two bounded metadata queries; never decrypt credentials for list rendering."""
    if not distributions:
        return {}
    items = db.execute(select(TaskItem.distribution_id, TaskItem.operation, TaskItem.status, TaskItem.error,
                              Task.kind.label('task_kind'), Task.id.label('task_id'))
                       .join(Task, Task.id == TaskItem.task_id)
                       .where(TaskItem.distribution_id.in_([dist.id for dist in distributions]))
                       .order_by(TaskItem.created_at.desc(), TaskItem.id.desc()))
    by_dist, latest = {}, {}
    for item in items:
        by_dist.setdefault(item.distribution_id, []).append(item)
        if item.task_kind == 'reupload':
            latest.setdefault(item.distribution_id, {'id': item.task_id, 'status': item.status, 'error': item.error})
    candidates, template_formats = {}, dict(formats)
    for template, fmt in db.execute(select(SiteUploadTemplate, CredentialFormat)
            .join(CredentialFormat, CredentialFormat.id == SiteUploadTemplate.format_id)
            .where(SiteUploadTemplate.site_id.in_({dist.site_id for dist in distributions}),
                   SiteUploadTemplate.category_id.in_({channel.category_id for channel in channels}))):
        candidates.setdefault((template.site_id, template.category_id), []).append(template)
        template_formats[fmt.id] = fmt
    channel_map = {channel.id: channel for channel in channels}
    result = {}
    for dist in distributions:
        channel = channel_map[dist.channel_id]
        reason = eligibility_reason(channel, dist, by_dist.get(dist.id, []))
        if reason is None:
            category, fmt = categories.get(channel.category_id), formats.get(channel.format_id)
            template, reason = choose_template(channel, category, fmt,
                candidates.get((dist.site_id, channel.category_id), []), template_formats)
            if reason is None:
                reason = configuration_reason(channel, sites.get(dist.site_id), category, fmt, template)
        result[dist.id] = {'reupload_available': reason is None, 'reupload_reason': reason,
                           'reupload_task': latest.get(dist.id),
                           **upload_state(dist, by_dist.get(dist.id, []))}
    return result


def reupload(db, actor, channel, payload):
    ids = payload.distribution_ids
    if (not ids or len(ids) != 1 or payload.site_ids is not None or payload.model is not None
            or payload.confirmation is not None or not payload.idempotency_key):
        raise HTTPException(422, '重新上传须选择一个具体分发并提供请求标识，不能指定站点、模型或删除确认')
    request_hash = fingerprint(json.dumps({'action': 'reupload', 'channel_id': channel.id,
                                          'distribution_ids': ids}, sort_keys=True))
    previous = db.scalar(select(Task).where(Task.actor_id == actor.id, Task.idempotency_key == payload.idempotency_key))
    if previous:
        assert_owner(db, actor, previous.owner_id)
        if previous.kind != 'reupload' or previous.owner_id != channel.owner_id or previous.request_hash != request_hash:
            raise HTTPException(409, '该请求标识已用于不同操作，请刷新后重新提交')
        return previous
    try:
        return _prepare(db, actor, channel, ids[0], payload.idempotency_key, request_hash)
    except OperationalError as exc:
        if getattr(exc.orig, 'sqlstate', getattr(exc.orig, 'pgcode', None)) != '55P03':
            raise
        db.rollback()
        raise HTTPException(409, '分发、原任务或模板正在处理，请刷新后重新上传') from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '重新上传请求已提交或记录已变化，请刷新后查看原任务') from None


def _prepare(db, actor, channel, distribution_id, nonce, request_hash):
    # The route owns owner/Channel locks. Reverse-order catalog/task callers
    # are never waited on: NOWAIT makes retry/reprepare/purge mutually exclusive
    # without a Channel->Task vs Task->Channel deadlock.
    channel = db.scalar(select(Channel).where(Channel.id == channel.id).with_for_update()
                        .execution_options(populate_existing=True))
    dist = db.scalar(select(Distribution).where(Distribution.id == distribution_id,
                                                 Distribution.channel_id == channel.id))
    if not dist:
        raise HTTPException(422, '所选分发不属于此渠道')
    task_ids = select(TaskItem.task_id).where(TaskItem.distribution_id == dist.id)
    old_tasks = list(db.scalars(select(Task).where(Task.id.in_(task_ids)).order_by(Task.id)
                               .with_for_update(nowait=True).execution_options(populate_existing=True)))
    items = list(db.scalars(select(TaskItem).where(TaskItem.distribution_id == dist.id).order_by(TaskItem.id)
                           .with_for_update(nowait=True).execution_options(populate_existing=True)))
    dist = db.scalar(select(Distribution).where(Distribution.id == dist.id).with_for_update(nowait=True)
                     .execution_options(populate_existing=True))
    if not dist or dist.channel_id != channel.id:
        raise HTTPException(409, '分发关联已改变，请刷新')
    if reason := eligibility_reason(channel, dist, items):
        raise HTTPException(409, reason)
    if any(item.channel_id != channel.id or item.site_id != dist.site_id for item in items):
        raise HTTPException(409, '原任务的分发关联不一致，请先核实')
    category = db.scalar(select(Category).where(Category.id == channel.category_id).with_for_update(nowait=True)
                         .execution_options(populate_existing=True))
    formats = {fmt.id: fmt for fmt in db.scalars(select(CredentialFormat)
        .where(CredentialFormat.category_id == channel.category_id).order_by(CredentialFormat.id)
        .with_for_update(nowait=True).execution_options(populate_existing=True))}
    templates = list(db.scalars(select(SiteUploadTemplate).where(SiteUploadTemplate.site_id == dist.site_id,
        SiteUploadTemplate.category_id == channel.category_id).order_by(SiteUploadTemplate.id)
        .with_for_update(nowait=True).execution_options(populate_existing=True)))
    site = db.scalar(select(Site).where(Site.id == dist.site_id).with_for_update(nowait=True)
                     .execution_options(populate_existing=True))
    fmt = formats.get(channel.format_id)
    template, reason = choose_template(channel, category, fmt, templates, formats)
    if reason is None:
        reason = configuration_reason(channel, site, category, fmt, template)
    if reason:
        raise HTTPException(422, reason)
    if not db.scalar(select(KeyVersion.id).where(KeyVersion.channel_id == channel.id,
                                                  KeyVersion.version == channel.key_version)):
        raise HTTPException(409, '当前密钥版本不存在，请先核实渠道')
    try:
        part = partition_for(channel, fmt, dist.partition_key, site=site)
        proxy = (encrypt(part['entries'][0]['proxy']) if part['entries'][0]['proxy'] else None
                 ) if channel.key_mode == 'multiple' else channel.proxy_encrypted
        history, snap = distribution_template_snapshot(channel, fmt, site, template, category=category,
            partition_key=dist.partition_key, partition_proxy_encrypted=proxy)
        issues = template_issues(template, site, category, fmt, remark_length=len(snap['remark']), effective=snap)
        issues.extend(target_issues(site, fmt, snap['models'], proxy=snap.get('proxy_requested', False),
            rpm=snap.get('rpm_enabled', False), routing_group=snap['routing_group'],
            remark_length=len(snap['remark']), channel_config=snap['channel_config'],
            wire_schema=snap.get('wire_format_schema')))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, '当前密钥分区或模板参数不可用，请联系管理员检查') from None
    if issues:
        raise HTTPException(422, '当前密钥分区与站点模板不兼容，请联系管理员检查')
    failed = [item for item in items if item.operation == 'create' and item.status == 'failed']
    task = new_task(db, actor, channel.owner_id, 'reupload', group_id=channel.group_id,
        idempotency_key=nonce, request_hash=request_hash,
        snapshot={'upload_mode': 'template', 'source_item_ids': [item.id for item in failed], 'site_ids': [site.id]})
    snap['reupload'] = True
    db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                   operation='create', key_version=channel.key_version, snapshot=snap, proxy_encrypted=proxy))
    dist.models, dist.routing_group, dist.key_version = snap['models'], snap['routing_group'], channel.key_version
    dist.upload_template_id, dist.template_version, dist.template_snapshot = template.id, template.version, history
    dist.status, dist.error = 'pending', None
    for item in failed:
        # Preserve old keys, attempted-write flags, frozen configuration and the
        # original failure evidence. Existing retry/reprepare exclude this stage.
        item.status, item.stage = 'cancelled', 'superseded'
    db.flush()
    for previous in old_tasks:
        refresh_task(db, previous)
    refresh_task(db, task)
    audit(db, actor, 'channel.reupload', 'channel', channel.id, {'task_id': task.id, 'distribution_id': dist.id})
    db.commit()
    return task
