"""Explicit, scoped database deletion; never an upstream operation."""
from fastapi import HTTPException
from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.exc import IntegrityError, OperationalError

from .auth import audit
from .channel_service import refresh_task
from .db import utcnow
from .deletion_locks import lock_local_purge
from .models import AuditEvent
from .models_billing import SettlementOrderLine, UsageFact
from .models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)


def references(value, ids):
    if isinstance(value, dict):
        return any(references(item, ids) for item in value.values())
    if isinstance(value, list):
        return any(references(item, ids) for item in value)
    return isinstance(value, str) and value in ids


_OMIT = object()
_ENTITY_KEYS = {'id', 'channel_id', 'distribution_id', 'task_id', 'item_id', 'fact_id', 'group_id', 'adopted_channel_id'}


def prune_references(value, ids, *, nested=False):
    """Remove exact structured identifiers, not matching text or other entities."""
    if isinstance(value, dict):
        if nested and any(key in _ENTITY_KEYS and isinstance(item, str) and item in ids
                          for key, item in value.items()):
            return _OMIT
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and item in ids:
                continue
            cleaned = prune_references(item, ids, nested=True)
            if cleaned is not _OMIT:
                result[key] = cleaned
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str) and item in ids:
                continue
            cleaned = prune_references(item, ids, nested=True)
            if cleaned is not _OMIT:
                result.append(cleaned)
        return result
    return value


def purge_distributions(db, actor, channel, ids, *, preserve_item=None, validate_only=False):
    """The caller owns Channel/owner locks. All conflicting work locks fail fast."""
    try:
        return _purge_distributions(db, actor, channel, ids, preserve_item=preserve_item, validate_only=validate_only)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '关联记录已变化或仍有未处理的引用，本次未删除任何数据，请刷新后检查') from None
    except OperationalError as exc:
        if getattr(exc.orig, 'sqlstate', getattr(exc.orig, 'pgcode', None)) != '55P03':
            raise
        db.rollback()
        raise HTTPException(409, '所选分发或关联记录正在处理，请稍后重新删除') from None


def _purge_distributions(db, actor, channel, ids, *, preserve_item=None, validate_only=False):
    lock_local_purge(db)
    targets = list(db.scalars(select(Distribution).where(
        Distribution.channel_id == channel.id, Distribution.id.in_(ids))))
    if len(targets) != len(ids):
        raise HTTPException(422, '目标不属于此渠道的现有分发记录')
    last = not db.scalar(select(Distribution.id).where(
        Distribution.channel_id == channel.id, Distribution.id.not_in(ids)).limit(1))
    if last and db.scalar(select(SettlementOrderLine.id).where(
            SettlementOrderLine.channel_id == channel.id).limit(1)):
        raise HTTPException(409, '渠道已关联结算单，请保留渠道及历史明细，可停用或归档；本次未删除任何数据')
    item_scope = TaskItem.distribution_id.in_(ids)
    if last:
        item_scope = or_(item_scope, TaskItem.channel_id == channel.id)
    task_ids = select(TaskItem.task_id).where(item_scope)
    tasks = list(db.scalars(select(Task).where(Task.id.in_(task_ids)).order_by(Task.id).with_for_update(nowait=True)))
    items = list(db.scalars(select(TaskItem).where(item_scope).order_by(TaskItem.id)
        .with_for_update(nowait=True).execution_options(populate_existing=True)))
    targets = list(db.scalars(select(Distribution).where(Distribution.id.in_(ids))
        .order_by(Distribution.id).with_for_update(nowait=True).execution_options(populate_existing=True)))
    if any(item.id != getattr(preserve_item, 'id', None) and item.status == 'running'
           and item.lease_until and item.lease_until > utcnow() for item in items):
        raise HTTPException(409, '所选分发正在执行，请等待当前请求结束后强制删除')
    facts = list(db.scalars(select(UsageFact).where(UsageFact.distribution_id.in_(ids))
        .order_by(UsageFact.id).with_for_update(nowait=True)))
    fact_ids = {row.id for row in facts}
    if validate_only:
        return {'targets': targets, 'items': items}

    if preserve_item is not None:
        if (preserve_item.operation != 'force_delete' or preserve_item not in items
                or preserve_item.snapshot.get('delete_acknowledged') is not True):
            raise HTTPException(409, '强制删除缺少已确认的远端结果，未清理本地记录')
        preserve_item.channel_id = preserve_item.distribution_id = preserve_item.key_version = None
        preserve_item.proxy_encrypted = None
        preserve_item.snapshot = {'operation_result': {
            'deleted_distribution_count': len(ids), 'deleted_channel_count': int(last), 'remote_confirmed': True}}
        items = [item for item in items if item.id != preserve_item.id]
        db.flush()

    removed = {*ids, *fact_ids, *(item.id for item in items)}
    removed.update(db.scalars(select(DistributionVersion.id).where(DistributionVersion.distribution_id.in_(ids))))
    pairs = [(dist.site_id, dist.remote_id) for dist in targets if dist.remote_id]
    unclaimed_scope = (tuple_(UnclaimedChannel.site_id, UnclaimedChannel.remote_id).in_(pairs)
                       if pairs else UnclaimedChannel.id.in_([]))
    if last:
        unclaimed_scope = or_(unclaimed_scope, UnclaimedChannel.adopted_channel_id == channel.id)
    removed.update(db.scalars(select(UnclaimedChannel.id).where(unclaimed_scope).with_for_update(nowait=True)))
    db.execute(delete(UnclaimedChannel).where(unclaimed_scope))
    db.execute(delete(TaskItem).where(TaskItem.id.in_([item.id for item in items])))
    db.execute(delete(DistributionVersion).where(DistributionVersion.distribution_id.in_(ids)))
    db.execute(delete(UsageFact).where(UsageFact.id.in_(fact_ids)))
    db.execute(delete(Distribution).where(Distribution.id.in_(ids)))
    deleted_tasks = set()
    for task in tasks:
        remaining = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id)))
        if not remaining:
            deleted_tasks.add(task.id)
            db.delete(task)
            continue
        snap = prune_references(task.snapshot, removed)
        site_ids = {item.site_id for item in remaining}
        if 'site_ids' in snap:
            snap['site_ids'] = [site_id for site_id in snap['site_ids'] if site_id in site_ids]
        if isinstance(snap.get('templates'), list):
            snap['templates'] = [template for template in snap['templates']
                                 if not isinstance(template, dict) or template.get('site_id') in site_ids]
        task.snapshot = snap
        refresh_task(db, task)
    removed.update(deleted_tasks)
    db.flush()
    group_id = channel.group_id
    group_deleted = False
    if last:
        removed.add(channel.id)
        for model in (ChannelCredential, KeyVersion):
            removed.update(db.scalars(select(model.id).where(model.channel_id == channel.id)))
        db.execute(delete(ChannelCredential).where(ChannelCredential.channel_id == channel.id))
        db.execute(delete(KeyVersion).where(KeyVersion.channel_id == channel.id))
        db.delete(channel)
        db.flush()
        if not db.scalar(select(Channel.id).where(Channel.group_id == group_id).limit(1)):
            for task in db.scalars(select(Task).where(Task.group_id == group_id).with_for_update(nowait=True)):
                task.group_id = None
                task.snapshot = {key: value for key, value in task.snapshot.items()
                                 if key not in ('group_tag', 'batch_fingerprint')}
            db.flush()
            db.execute(delete(UploadGroup).where(UploadGroup.id == group_id))
            removed.add(group_id)
            group_deleted = True
    # Cross-task references contain IDs, never the payload of a deleted item.
    for task in db.scalars(select(Task)):
        if references(task.snapshot, removed):
            task = db.scalar(select(Task).where(Task.id == task.id).with_for_update(nowait=True)
                             .execution_options(populate_existing=True))
            if task:
                task.snapshot = prune_references(task.snapshot, removed)
    for item in db.scalars(select(TaskItem)):
        if references(item.snapshot, removed) or any(
                item.error == '已按当前模板重建为新任务 ' + task_id for task_id in deleted_tasks):
            item = db.scalar(select(TaskItem).where(TaskItem.id == item.id).with_for_update(nowait=True)
                             .execution_options(populate_existing=True))
            if not item:
                continue
            item.snapshot = prune_references(item.snapshot, removed)
            if item.error and any(item.error == '已按当前模板重建为新任务 ' + task_id for task_id in deleted_tasks):
                item.error = '关联的新任务已删除'
    for event in db.scalars(select(AuditEvent)):
        if event.object_id not in removed and not references(event.summary, removed):
            continue
        event = db.scalar(select(AuditEvent).where(AuditEvent.id == event.id).with_for_update(nowait=True)
                          .execution_options(populate_existing=True))
        if not event:
            continue
        if event.object_id in removed or event.summary.get('item_id') in removed:
            db.delete(event)
        elif references(event.summary, removed):
            event.summary = prune_references(event.summary, removed)
    # Security accountability only: no deleted IDs, credential/config snapshots
    # or remote identifiers are retained in the new audit event.
    audit(db, actor, 'channel.force_delete' if preserve_item else 'channel.hard_delete_local', 'local_deletion', None,
          {'distribution_count': len(ids), 'channel_count': int(last)})
    return {'deleted_distribution_ids': ids, 'channel_deleted': last,
            'deleted_channel_id': channel.id if last else None, 'remote_confirmed': preserve_item is not None,
            'group_deleted': group_deleted}
