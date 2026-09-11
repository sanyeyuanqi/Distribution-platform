"""Local-first deletion with a credential-free, durable remote cleanup outbox."""
import hashlib

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from .adapters.newapi_builds import build_snapshot
from .adapters.silicon import RemoteError
from .auth import assert_owner, audit
from .channel_purge import purge_distributions
from .channel_service import api_json, new_task, refresh_task
from .distribution_tombstones import locally_deleted
from .models import CredentialFormat, Site, User
from .models_channels import (
    Distribution,
    DistributionVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
)
from .site_remote_identity import remote_identity_lock_key, same_deployment_site_ids

KIND = 'force_delete_async'
OPERATION = 'remote_cleanup'


def lock_target(db, site_id, remote_id):
    """Cleanup and binding share a fence across historical registrations."""
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404, '站点不存在')
    key = remote_identity_lock_key(site, 'keyacross:remote-cleanup', remote_id)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(key))):
        raise HTTPException(409, '远端目标正在处理，请稍后重试')


def lock_request(db, actor_id, nonce):
    key = int.from_bytes(hashlib.sha256(f'keyacross:cleanup-request:{actor_id}:{nonce}'.encode()).digest()[:8],
                         'big', signed=True)
    db.execute(select(func.pg_advisory_xact_lock(key)))


def is_reserved(db, site_id, remote_id):
    # Failed or cancelled cleanups remain explicit deletion intentions. A retry
    # must not erase a new adoption made while their worker was unavailable.
    return db.scalar(select(TaskItem.id).where(TaskItem.site_id.in_(same_deployment_site_ids(site_id)),
        TaskItem.operation == OPERATION,
        TaskItem.snapshot['delete_target']['id'].as_string() == str(remote_id),
        TaskItem.snapshot['delete_acknowledged'].as_boolean().is_not(True)).limit(1)) is not None


def cleanup_json(db, task):
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.created_at)))
    names = dict(db.execute(select(Site.id, Site.name).where(Site.id.in_({item.site_id for item in items}))).all())
    counts = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return api_json({**{key: getattr(task, key) for key in (
        'id', 'kind', 'status', 'created_at', 'updated_at', 'finished_at')},
        'total': len(items), 'counts': counts,
        'can_retry': task.status not in ('queued', 'running') and any(
            item.status in ('failed', 'needs_review', 'cancelled') for item in items),
        'result': {**task.snapshot.get('local_result', {}),
                   'remote_confirmed': bool(items) and all(item.snapshot.get('delete_acknowledged') is True for item in items)},
        'items': [{**{key: getattr(item, key) for key in (
            'id', 'site_id', 'status', 'stage', 'error', 'attempts', 'updated_at')},
            'site_name': names.get(item.site_id, ''), 'operation': OPERATION,
            'remote_confirmed': item.snapshot.get('delete_acknowledged') is True} for item in items]})


def enqueue_cleanup(db, actor, channel, payload, request_hash):
    try:
        return _enqueue_cleanup(db, actor, channel, payload, request_hash)
    except OperationalError as exc:
        if getattr(exc.orig, 'sqlstate', getattr(exc.orig, 'pgcode', None)) != '55P03':
            raise
        db.rollback()
        raise HTTPException(409, '账号或关联记录正在处理，请稍后重试；本次未删除任何本地记录') from None


def _enqueue_cleanup(db, actor, channel, payload, request_hash):
    ids = payload.distribution_ids
    if (not ids or len(set(ids)) != len(ids) or payload.site_ids is not None or payload.model is not None
            or not payload.idempotency_key):
        raise HTTPException(422, '请选择不重复的具体分发记录，并提供本次删除请求标识')
    if payload.confirmation != f'DELETE LOCAL AND QUEUE REMOTE {len(ids)}':
        raise HTTPException(422, '请重新确认立即删除本地记录并异步清理远端渠道')
    actor_version = actor.session_version
    current_actor = db.scalar(select(User).where(User.id == actor.id).with_for_update(key_share=True, nowait=True)
                              .execution_options(populate_existing=True))
    if not current_actor or not current_actor.active or current_actor.archived or current_actor.session_version != actor_version:
        raise HTTPException(403, '账号权限已改变，请重新登录后操作')
    assert_owner(db, current_actor, channel.owner_id)
    checked = purge_distributions(db, actor, channel, ids, validate_only=True)
    fmt = db.get(CredentialFormat, channel.format_id)
    task = new_task(db, actor, channel.owner_id, KIND)
    task.idempotency_key, task.request_hash = payload.idempotency_key, request_hash
    for dist in checked['targets']:
        site = db.get(Site, dist.site_id)
        related = [item for item in checked['items'] if item.distribution_id == dist.id]
        channel_type = (dist.remote_snapshot or {}).get('type') or (dist.template_snapshot or {}).get('channel_type')
        if type(channel_type) is not int:
            channel_type = (fmt.schema_config or {}).get('remote_type') if fmt else None
        target = {'id': str(dist.remote_id) if dist.remote_id else None, 'name': dist.remote_name, 'type': channel_type}
        if target['id']:
            lock_target(db, site.id, target['id'])
        snap = {'site_adapter': site.adapter, 'site_base_url': site.base_url,
                'seller_user_id': str(site.seller_user_id), **build_snapshot(site), 'delete_target': target,
                'remote_never_created': not dist.remote_id and dist.status in ('pending', 'failed', 'cancelled')
                    and not any((dist.remote_snapshot or {}).get(key) for key in ('id', 'name', 'type'))
                    and not any((item.remote_write_attempted
                        and not (item.operation == 'test' and (item.snapshot or {}).get('test_source') == 'local'))
                        or (item.operation == 'create' and (item.status == 'succeeded' or item.stage in (
                            'create_sent', 'created_pending_verification', 'reconcile'))) for item in related)
                    and not db.scalar(select(DistributionVersion.id).where(
                        DistributionVersion.distribution_id == dist.id).limit(1))}
        acknowledged = dist.status == 'deleted' and not locally_deleted(dist) and any(
            item.operation in ('delete_remote', 'force_delete') and item.snapshot.get('delete_acknowledged') is True
            and item.snapshot.get('delete_target') == target
            and all(item.snapshot.get(field) == snap[field] for field in ('site_adapter', 'site_base_url', 'seller_user_id'))
            for item in related)
        if acknowledged:
            snap['delete_acknowledged'] = True
        prior_delete = any(item.operation in ('delete_remote', 'force_delete') and item.remote_write_attempted for item in related)
        # No Channel/Distribution/Group/Key FK or deleted UUID in the outbox.
        db.add(TaskItem(task_id=task.id, site_id=site.id, operation=OPERATION, snapshot=snap,
                        remote_write_attempted=prior_delete and not acknowledged))
    try:
        db.flush()
        result = purge_distributions(db, actor, channel, ids)
        task.snapshot = {'local_result': {'local_deleted': True, 'deleted_distribution_count': len(ids),
                                       'deleted_channel_count': int(result['channel_deleted'])}}
        db.flush()
        refresh_task(db, task)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '删除请求已提交或记录已变化，请刷新后查看清理任务') from None
    return cleanup_json(db, task)


def retry_cleanup(db, actor, task):
    assert_owner(db, actor, task.owner_id)
    if task.status in ('queued', 'running'):
        raise HTTPException(409, '远端清理仍在执行，请等待当前任务结束')
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.id)
                           .with_for_update().execution_options(populate_existing=True)))
    candidates = [item for item in items if item.status in ('failed', 'needs_review', 'cancelled')]
    if not candidates:
        raise HTTPException(409, '没有需要重试的远端清理步骤')
    for item in candidates:
        # Explicit retry authorizes another DELETE only after a fresh exact-ID,
        # identity and ownership read. Automatic recovery never grants this.
        item.snapshot = {**item.snapshot, 'retry_delete_authorized': True}
        item.status, item.error, item.stage = 'pending', None, 'queued'
        item.attempts, item.lease_until, item.next_attempt_at = 0, None, None
    task.cancelled = False
    task.execution_actor_id, task.actor_session_version = actor.id, actor.session_version
    refresh_task(db, task)
    audit(db, actor, 'remote_cleanup.retry', 'task', task.id, {'item_count': len(candidates)})
    db.commit()
    return cleanup_json(db, task)


def assert_cleanup(db, task, item):
    from .worker import WorkRemoved, WriteStopped

    with db.no_autoflush:
        if not db.scalar(select(TaskItem.id).where(TaskItem.id == item.id)):
            raise WorkRemoved('远端清理任务已不存在')
        task = db.get(Task, item.task_id, populate_existing=True)
        actor = db.get(User, task.execution_actor_id or task.actor_id, populate_existing=True) if task else None
        site = db.get(Site, item.site_id, populate_existing=True)
    if (not task or task.kind != KIND or item.channel_id or item.distribution_id or item.key_version
            or not actor or not actor.active or actor.archived or actor.session_version != task.actor_session_version):
        raise WriteStopped('清理发起账号或任务权限已改变；本地已删除，远端尚未确认清理')
    try:
        assert_owner(db, actor, task.owner_id)
    except HTTPException:
        raise WriteStopped('已没有此清理任务的管理权限；远端尚未确认清理') from None
    if task.cancelled:
        raise WriteStopped('远端清理已取消；本地记录已删除')
    if item.snapshot.get('delete_acknowledged') is True or item.snapshot.get('remote_never_created') is True:
        return site
    if not site or any(item.snapshot.get(key) != value for key, value in (
            ('site_adapter', site.adapter), ('site_base_url', site.base_url), ('seller_user_id', str(site.seller_user_id)))):
        raise WriteStopped('站点连接身份已改变；本地已删除，远端清理已停止')
    if 'site_build_id' in item.snapshot and item.snapshot['site_build_id'] != build_snapshot(site).get('site_build_id'):
        raise WriteStopped('站点构建与冻结任务不匹配；本地已删除，远端清理已停止')
    return site


def assert_unclaimed_target(db, site_id, remote_id):
    """Called under the target lock again immediately before transport."""
    site_ids = same_deployment_site_ids(site_id)
    if (db.scalar(select(Distribution.id).where(Distribution.site_id.in_(site_ids), Distribution.remote_id == remote_id).limit(1))
            or db.scalar(select(UnclaimedChannel.id).where(UnclaimedChannel.site_id.in_(site_ids),
                UnclaimedChannel.remote_id == remote_id, UnclaimedChannel.adopted_channel_id.is_not(None)).limit(1))):
        raise RemoteError('此远端渠道已有新的本地归属，已停止清理，请人工核实', unknown=True)


def execute_cleanup(db, item, lease_check):
    from .force_deletion import remote_delete_target
    from .worker import get_adapter, lock_result_distribution

    task = db.get(Task, item.task_id)
    site = assert_cleanup(db, task, item)
    lease_check()
    if item.snapshot.get('delete_acknowledged') is True:
        return
    if item.snapshot.get('remote_never_created') is True:
        item.snapshot = {**item.snapshot, 'delete_acknowledged': True}
        db.commit()
        return
    target = item.snapshot.get('delete_target') or {}

    def before_write():
        lease_check()
        assert_cleanup(db, task, item)
        item.stage, item.remote_write_attempted = 'write_sent', True
        item.snapshot = {**item.snapshot, 'retry_delete_authorized': False}
        db.commit()
        lock_result_distribution(db, item)
        lock_target(db, site.id, target['id'])
        assert_cleanup(db, task, item)
        assert_unclaimed_target(db, site.id, target['id'])
        # Retain TaskItem + target locks through the actual HTTP request.

    adapter = get_adapter(site, before_write=before_write)
    if 'site_build_id' in item.snapshot:
        adapter.frozen_build_id = item.snapshot['site_build_id']
    if not target.get('id'):
        try:
            found = adapter.find_unique_name(target.get('name') or '')
        except RemoteError:
            raise RemoteError('本地已删除，历史创建结果不明且无法按名称核实；未发送远端删除', unknown=True) from None
        raise RemoteError('本地已删除，' + ('发现同名远端但缺少已确认的关联 ID，不能自动删除' if found
            else '未找到同名远端，但历史创建结果仍无法确认；未发送远端删除'), unknown=True)
    try:
        lock_target(db, site.id, target['id'])
        assert_unclaimed_target(db, site.id, target['id'])
        remote = remote_delete_target(adapter, target)
        if remote is not None:
            if item.remote_write_attempted and item.snapshot.get('retry_delete_authorized') is not True:
                raise RemoteError('本地已删除，已核实远端仍存在；上次删除结果不明，请明确重试清理', unknown=True)
            adapter.delete(target['id'], expected=target)
    except HTTPException:
        raise RemoteError('远端目标正在处理，本地已删除；将重试清理', retryable=True) from None
    except RemoteError as exc:
        # Existing synchronous-delete helpers intentionally mention retained
        # local data; never reuse that message for this local-first action.
        message = ('本地已删除，远端身份或归属不一致，已停止清理' if exc.category == 'identity_mismatch'
                   else '本地已删除，远端清理尚未确认；请检查站点权限或连接后重试')
        if str(exc).startswith(('本地已删除，', '此远端渠道已有新的本地归属')):
            message = str(exc)
        raise RemoteError(message, unknown=exc.unknown or item.remote_write_attempted,
                          retryable=exc.retryable, category=exc.category, status_code=exc.status_code) from None
    lock_result_distribution(db, item)
    lease_check()
    item.snapshot = {**item.snapshot, 'delete_acknowledged': True, 'retry_delete_authorized': False}
    item.stage = 'remote_cleanup_confirmed'
    db.commit()
