"""Confirm an explicitly selected remote deletion before purging its local data."""
from fastapi import HTTPException
from sqlalchemy import select

from .adapters.new_api import NewAPIAdapter
from .adapters.newapi_builds import BUILD_ID
from .adapters.newapi_compatibility import RELEASES
from .adapters.silicon import RemoteError, SiliconAdapter, validate_delete_identity
from .adapters.tcp_red import TcpRedAdapter
from .channel_purge import purge_distributions
from .models_channels import Channel, DistributionVersion


def remote_delete_target(adapter, expected):
    permissions = adapter.permissions()
    if permissions.get('can_write') is not True:
        raise RemoteError('站点没有删除此渠道的权限，本地记录已保留', category='permission_denied')
    if isinstance(adapter, SiliconAdapter) and not isinstance(adapter, TcpRedAdapter):
        identity = adapter.request('GET', '/api/user/self')
        if (not isinstance(identity, dict) or str(identity.get('id')) != str(adapter.site.seller_user_id)):
            raise RemoteError('站点账号身份无法确认，本地记录已保留', category='identity_mismatch')
    remote_id = SiliconAdapter._observation_id(expected['id'])
    try:
        if isinstance(adapter, NewAPIAdapter):
            # Official GetChannel and this exact build's authenticated read probe
            # return GORM's precise not-found response. Do not inherit this proof
            # for a future build; a proxy/permission 404 never proves absence.
            payload = adapter._request_envelope('GET', '/api/channel/' + remote_id,
                                                allow_business_failure=True)
            if payload.get('success') is False:
                reviewed_not_found = adapter._current_version in RELEASES or adapter._current_version == BUILD_ID
                if reviewed_not_found and payload.get('message') == 'record not found' and payload.get('data') in (None, {}):
                    return None
                raise RemoteError('站点未提供可确认的渠道不存在证据', unknown=True)
            remote = payload.get('data')
        else:
            remote = adapter.detail(remote_id)
    except RemoteError as exc:
        raise RemoteError('无法确认远端渠道是否存在或有权查看，本地记录已保留；请核实后继续',
                          unknown=True, category=exc.category, status_code=exc.status_code) from None
    validate_delete_identity(remote, expected)
    adapter._observation_owned(remote, permissions)
    return remote


def execute_force_delete(db, adapter, task, item, dist, actor, lease_check):
    # Local imports avoid sharing edits with the independent model-test worker.
    from .worker import WriteStopped, assert_execution, lock_result_distribution

    lease_check()
    assert_execution(db, task, item)
    channel = db.scalar(select(Channel).where(Channel.id == item.channel_id).with_for_update())
    if not channel:
        raise WriteStopped('本地渠道已不存在，停止强制删除')
    try:
        purge_distributions(db, actor, channel, [dist.id], preserve_item=item, validate_only=True)
    except HTTPException as exc:
        raise RemoteError(str(exc.detail), category='local_deletion_blocked') from None
    db.commit()

    acknowledged = item.snapshot.get('delete_acknowledged') is True
    if not acknowledged:
        target = item.snapshot['delete_target']
        if not target.get('id'):
            if item.snapshot.get('remote_never_created') is True:
                remote = None
            else:
                found = adapter.find_unique_name(target['name'])
                if not found:
                    raise RemoteError('历史创建结果不明，按名称仍无法确认远端是否存在；本地记录已保留', unknown=True)
                target = {**target, 'id': str(found['id'])}
                remote = remote_delete_target(adapter, target)
                dist = lock_result_distribution(db, item)
                dist.remote_id = target['id']
                item.snapshot = {**item.snapshot, 'delete_target': target}
                db.commit()
        else:
            remote = remote_delete_target(adapter, target)
        if remote is not None:
            if item.remote_write_attempted or item.stage in ('write_sent', 'reconcile'):
                raise RemoteError('已核实远端渠道仍存在；未自动重复删除，请重新明确发起强制删除', unknown=True)
            lease_check()
            adapter.delete(target['id'], expected=target)
        dist = lock_result_distribution(db, item)
        # Persist the positive outcome before local cleanup. A blocked purge or
        # process restart must never repeat the already-acknowledged DELETE.
        item.snapshot = {**item.snapshot, 'delete_acknowledged': True}
        item.stage = 'remote_deleted_pending_purge'
        dist.status, dist.error = 'deleted', None
        for version in db.scalars(select(DistributionVersion).where(
                DistributionVersion.distribution_id == dist.id, DistributionVersion.valid_to.is_(None))):
            from .db import utcnow
            version.valid_to = utcnow()
        db.commit()

    lease_check()
    assert_execution(db, task, item)
    channel = db.scalar(select(Channel).where(Channel.id == item.channel_id).with_for_update())
    try:
        purge_distributions(db, actor, channel, [dist.id], preserve_item=item)
    except HTTPException as exc:
        raise RemoteError('远端已确认删除，但本地记录暂未清理：' + str(exc.detail),
                          category='local_deletion_blocked') from None
    db.commit()
