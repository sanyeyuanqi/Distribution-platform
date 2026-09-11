# FastAPI intentionally declares dependency callables in defaults.
# ruff: noqa: B008
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import audit, require_roles, scope_owner_ids
from ..channel_service import (
    check_no_pending,
    get_task,
    new_task,
    notify_worker,
    refresh_task,
    task_json,
)
from ..credential_containers import partition_for, primary_credential
from ..db import get_db
from ..distribution_monitoring import MONITOR_OPERATIONS, record_observation
from ..distribution_tombstones import locally_deleted
from ..models import Category, CredentialFormat, Site, SiteUploadTemplate, User
from ..models_channels import Channel, Distribution, Task, TaskItem
from ..security import encrypt
from ..upload_templates import (
    distribution_template_snapshot,
    effective_template,
    same_format_type,
    template_issues,
    template_variant,
)

router = APIRouter(tags=['tasks'])


def reject_sync_resubmission(db, task):
    # Historical observations and already queued work remain readable/executable;
    # only the background scheduler may request another synchronization.
    if task.kind in ('sync', 'scheduled_sync', 'sync_usage') or db.scalar(
            select(TaskItem.id).where(TaskItem.task_id == task.id,
                                      TaskItem.operation.in_(('sync', 'sync_usage'))).limit(1)):
        raise HTTPException(409, '同步由后台定时执行，请等待下一次自动同步')


def lock_monitor_channels(db, items, task_id):
    channel_ids = sorted({item.channel_id for item in items
                          if (item.operation in MONITOR_OPERATIONS or item.operation == 'force_delete') and item.channel_id})
    for channel_id in channel_ids:
        channel = db.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
        if channel and any(item.channel_id == channel_id and item.operation in MONITOR_OPERATIONS for item in items):
            check_no_pending(db, channel, allow_uncertain_tests=True, exclude_task_id=task_id)


def check_live_distributions(db, items, *, lock=True):
    ids = {item.distribution_id for item in items if item.distribution_id}
    query = select(Distribution).where(Distribution.id.in_(ids)).order_by(Distribution.id)
    if lock:
        query = query.with_for_update()
    rows = db.scalars(query.execution_options(populate_existing=True))
    removed = {dist.id for dist in rows if locally_deleted(dist)}
    live = [item for item in items if item.distribution_id not in removed or item.operation == 'force_delete']
    if not live:
        raise HTTPException(409, '任务分发已强制移除本地关联，不能重试或恢复该分发')
    return live


@router.get('/tasks')
def tasks(scope: str = 'team', owner_id: str | None = None, limit: int = 100,
          user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    owners = scope_owner_ids(db, user, scope, owner_id)
    rows = list(db.scalars(select(Task).where(Task.owner_id.in_(owners)).order_by(Task.created_at.desc()).limit(min(500, max(1, limit)))))
    counts = {}
    for task_id, status, count in db.execute(select(TaskItem.task_id, TaskItem.status, func.count())
                                            .where(TaskItem.task_id.in_([t.id for t in rows]))
                                            .group_by(TaskItem.task_id, TaskItem.status)):
        counts.setdefault(task_id, {})[status] = count
    return {'items': [task_json(db, task, counts=counts.get(task.id, {})) for task in rows], 'total': len(rows)}


@router.get('/tasks/{task_id}')
def detail(task_id: str, user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    return task_json(db, get_task(db, user, task_id), details=True)


@router.post('/tasks/{task_id}/retry')
def retry(task_id: str, user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    task = get_task(db, user, task_id, lock=True)
    reject_sync_resubmission(db, task)
    if task.kind == 'force_delete_async':
        raise HTTPException(409, '请在渠道页面的远端清理任务中重试，先核实远端结果')
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id, TaskItem.status == 'failed').with_for_update()))
    if not items:
        raise HTTPException(409, '没有可重试失败项；待核实新增不能直接重发，请先核对远端结果')
    if task.status in ('running', 'queued'):
        raise HTTPException(409, '任务仍在执行，请等待当前任务结束')
    lock_monitor_channels(db, items, task.id)
    items = check_live_distributions(db, items)
    for item in items:
        if item.operation == 'test' and item.remote_write_attempted:
            raise HTTPException(409, '此模型测试已经发送，不能重放；请在渠道详情明确发起新的测试')
        # A retry of an old version after rotation could roll back live credentials.
        channel = db.get(Channel, item.channel_id) if item.channel_id else None
        if channel and item.operation != 'force_delete' and item.key_version != channel.key_version:
            raise HTTPException(409, '渠道凭据版本已变化，不能重放旧版本任务')
        stage = ('remote_deleted_pending_purge' if item.snapshot.get('delete_acknowledged') is True
                 else 'reconcile' if item.remote_write_attempted else 'queued') if item.operation == 'force_delete' else 'queued'
        item.status, item.stage, item.error = 'pending', stage, None
        item.attempts, item.lease_until, item.next_attempt_at = 0, None, None
        if item.distribution_id:
            record_observation(db.get(Distribution, item.distribution_id), item, replace=True)
    task.execution_actor_id, task.actor_session_version = user.id, user.session_version
    task.cancelled = False
    refresh_task(db, task)
    audit(db, user, 'task.retry', 'task', task.id, {'failed_items': len(items)})
    db.commit()
    notify_worker()
    return task_json(db, task, details=True)


@router.post('/tasks/{task_id}/reconcile')
def reconcile(task_id: str, user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    task = get_task(db, user, task_id, lock=True)
    reject_sync_resubmission(db, task)
    if task.kind == 'force_delete_async':
        raise HTTPException(409, '请在渠道页面的远端清理任务中重试，先核实远端结果')
    if task.status in ('running', 'queued'):
        raise HTTPException(409, '任务仍在执行')
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id, TaskItem.status == 'needs_review').with_for_update()))
    if not items:
        raise HTTPException(409, '没有待核实项')
    lock_monitor_channels(db, items, task.id)
    items = check_live_distributions(db, items)
    for item in items:
        item.status, item.error = 'pending', None
        if item.stage not in ('created_pending_verification', 'updated_pending_verification'):
            item.stage = 'reconcile'
        item.next_attempt_at = None
        if item.distribution_id:
            record_observation(db.get(Distribution, item.distribution_id), item)
    task.execution_actor_id, task.actor_session_version = user.id, user.session_version
    # Reconciliation is read-only, even for a cancelled task.
    refresh_task(db, task)
    audit(db, user, 'task.reconcile', 'task', task.id, {'items': len(items)})
    db.commit()
    notify_worker()
    return task_json(db, task, details=True)


@router.post('/tasks/{task_id}/reprepare-templates')
def reprepare_templates(task_id: str, user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    selected = get_task(db, user, task_id)
    db.scalar(select(User).where(User.id == selected.owner_id).with_for_update(key_share=True))
    previous = get_task(db, user, task_id, lock=True)
    if previous.status in ('running', 'queued'):
        raise HTTPException(409, '任务仍在执行，请等待当前任务结束')
    candidates = list(db.scalars(select(TaskItem).where(TaskItem.task_id == previous.id,
        TaskItem.operation == 'create', TaskItem.status.in_(['failed', 'cancelled']),
        TaskItem.remote_write_attempted.is_(False),
        TaskItem.stage.in_(['queued', 'validation', 'cancelled'])).with_for_update()))
    candidates = [item for item in candidates if item.snapshot.get('upload_template_id')]
    if not candidates:
        raise HTTPException(409, '没有可按模板重建的未发送项；已发送或待核实的远端写入不能重新创建')
    candidates = check_live_distributions(db, candidates, lock=False)
    prepared = []
    for item in candidates:
        channel = db.scalar(select(Channel).where(Channel.id == item.channel_id).with_for_update())
        dist = db.scalar(select(Distribution).where(Distribution.id == item.distribution_id).with_for_update())
        if locally_deleted(dist):
            raise HTTPException(409, '此分发已强制移除本地关联，不能按模板重建')
        if not channel or channel.archived or channel.upload_mode != 'template' or not dist or dist.remote_id:
            raise HTTPException(409, '渠道已归档或已有远端关联，不能重新创建')
        historical_items = db.scalars(select(TaskItem).where(TaskItem.distribution_id == dist.id,
            or_(TaskItem.remote_write_attempted.is_(True), TaskItem.stage.in_(
                ['create_sent', 'write_sent', 'created_pending_verification', 'updated_pending_verification', 'reconcile', 'complete']))))
        historical_write = any(not (old.operation == 'test' and (old.snapshot or {}).get('test_source') == 'local')
                               for old in historical_items)
        if historical_write:
            raise HTTPException(409, '此分发曾尝试过远端写入，必须先核实远端结果，不能按模板重新创建')
        unresolved = db.scalar(select(TaskItem.id).where(TaskItem.distribution_id == dist.id,
            TaskItem.id != item.id, TaskItem.status.in_(['pending', 'running', 'needs_review'])).limit(1))
        if unresolved:
            raise HTTPException(409, '此分发已有执行中或待核实任务，不能重新创建')
        category = db.get(Category, channel.category_id)
        channel_format = db.get(CredentialFormat, channel.format_id)
        candidates_for_site = db.scalars(select(SiteUploadTemplate).where(SiteUploadTemplate.site_id == item.site_id,
            SiteUploadTemplate.category_id == channel.category_id))
        template = next((candidate for candidate in candidates_for_site
                         if template_variant(category, db.get(CredentialFormat, candidate.format_id), candidate.models)
                         == template_variant(category, channel_format, channel.models)), None)
        if not template or not template.enabled or not same_format_type(
                db.get(CredentialFormat, template.format_id), db.get(CredentialFormat, channel.format_id)):
            raise HTTPException(422, '原目标站点没有已启用且格式匹配的分类模板，请联系管理员配置')
        site, fmt = db.get(Site, item.site_id), db.get(CredentialFormat, channel.format_id)
        effective_remark = channel.remark or template.remark
        try:
            part = partition_for(channel, fmt, dist.partition_key, site=site)
            proxy = (encrypt(part['entries'][0]['proxy']) if part['entries'][0]['proxy'] else None
                     ) if channel.key_mode == 'multiple' else channel.proxy_encrypted
            effective = effective_template(template, channel.upload_settings, proxy, category=category,
                                           fmt=fmt, credential=primary_credential(channel, fmt, dist.partition_key, site=site))
        except ValueError:
            raise HTTPException(422, '原目标站点的模板参数暂不可用，请联系管理员检查') from None
        issues = template_issues(template, site, db.get(Category, channel.category_id), fmt,
                                 remark_length=len(effective_remark), effective=effective)
        if issues:
            raise HTTPException(422, '原目标站点的模板配置暂不可用，请联系管理员检查')
        prepared.append((item, channel, dist, template, site, fmt, effective_remark, proxy))
    task = new_task(db, user, previous.owner_id, 'upload', group_id=previous.group_id,
                    snapshot={'upload_mode': 'template', 'source_task_id': previous.id,
                              'site_ids': list(dict.fromkeys(item.site_id for item in candidates))})
    for old_item, channel, dist, template, site, fmt, note, proxy in prepared:
        history, snap = distribution_template_snapshot(channel, fmt, site, template,
                category=db.get(Category, channel.category_id), partition_key=dist.partition_key, partition_proxy_encrypted=proxy)
        dist.models, dist.routing_group = snap['models'], template.routing_group
        dist.key_version, dist.upload_template_id, dist.template_version = channel.key_version, template.id, template.version
        dist.template_snapshot = history
        dist.status, dist.error = 'pending', None
        db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                        operation='create', key_version=channel.key_version, snapshot=snap,
                        proxy_encrypted=proxy))
        old_item.status, old_item.stage, old_item.error = 'cancelled', 'superseded', '已按当前模板重建为新任务 ' + task.id
    db.flush()
    refresh_task(db, previous)
    refresh_task(db, task)
    audit(db, user, 'task.reprepare_templates', 'task', task.id,
          {'source_task_id': previous.id, 'items': len(prepared)})
    db.commit()
    notify_worker()
    return task_json(db, task, details=True)


@router.post('/tasks/{task_id}/cancel')
def cancel(task_id: str, user=Depends(require_roles('superadmin', 'user')), db: Session = Depends(get_db)):
    task = get_task(db, user, task_id, lock=True)
    task.cancelled = True
    for item in db.scalars(select(TaskItem).where(TaskItem.task_id == task.id, TaskItem.status == 'pending')):
        item.status, item.error = 'cancelled', '发起人取消了未开始步骤'
        if not item.remote_write_attempted and item.stage in ('queued', 'validation'):
            item.stage = 'cancelled'
        elif item.remote_write_attempted or item.stage in ('create_sent', 'write_sent', 'created_pending_verification', 'updated_pending_verification', 'reconcile'):
            item.status, item.error = 'needs_review', '任务已取消；此前远端写入仍需核实，不会重复创建'
        if item.distribution_id:
            record_observation(db.get(Distribution, item.distribution_id), item)
    refresh_task(db, task)
    audit(db, user, 'task.cancel', 'task', task.id)
    db.commit()
    return task_json(db, task, details=True)
