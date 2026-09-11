"""Archive a local channel only after every linked remote is confirmed disabled."""
from fastapi import HTTPException
from sqlalchemy import select

from .adapters import get_adapter, supports_adapter
from .adapters.silicon import RemoteError
from .db import utcnow
from .distribution_status import observed_remote_state
from .distribution_tombstones import locally_deleted
from .models import Site
from .models_channels import Distribution
from .remote_channel_status import remote_channel_state


def archive_target(dist):
    snapshot = dist.remote_snapshot or {}
    if (not dist.remote_id or dist.status == 'deleted' or not isinstance(snapshot, dict)
            or locally_deleted(dist) or type(snapshot.get('id')) not in (int, str)
            or str(snapshot['id']) != str(dist.remote_id) or not dist.remote_name
            or snapshot.get('name') != dist.remote_name or type(snapshot.get('type')) is not int):
        return None
    return {'id': str(dist.remote_id), 'name': dist.remote_name, 'type': snapshot['type']}


def archive_projection(channel, distributions):
    reason = ''
    if channel.archived:
        reason = '渠道已归档'
    elif not distributions:
        reason = '没有已确认停用的远端渠道，不能归档'
    elif any(archive_target(dist) is None for dist in distributions):
        reason = '存在未关联、已删除或身份待核实的远端渠道，不能归档'
    elif any(observed_remote_state(dist)['remote_status'] != 'disabled' for dist in distributions):
        reason = '所有远端分发均确认停用后才可归档；请先停用并同步状态'
    return {'archive_available': not reason, 'archive_reason': reason}


def verify_archive(db, channel):
    """The caller holds the channel lock and has rejected unfinished operations.

    Read all distributions, regardless of any UI selection. Keep observations and
    the archive flag unchanged unless all remote identities and states pass.
    """
    if channel.archived:
        raise HTTPException(409, '渠道已归档')
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == channel.id)
        .order_by(Distribution.id).with_for_update().execution_options(populate_existing=True)))
    if not distributions:
        raise HTTPException(409, '没有已确认停用的远端渠道，不能归档')
    targets = [archive_target(dist) for dist in distributions]
    if any(target is None for target in targets):
        raise HTTPException(409, '存在未关联、已删除或身份待核实的远端渠道，不能归档')
    sites = {site.id: site for site in db.scalars(select(Site)
        .where(Site.id.in_({dist.site_id for dist in distributions})).order_by(Site.id).with_for_update(key_share=True))}
    adapters, checks = {}, []
    for dist, target in zip(distributions, targets, strict=True):
        site = sites.get(dist.site_id)
        if site is None or not supports_adapter(site.adapter):
            raise HTTPException(409, '站点不存在或尚未适配，无法确认远端停用，不能归档')
        try:
            if site.id not in adapters:
                adapters[site.id] = get_adapter(site)
            raw_status = adapters[site.id].channel_status(dist.remote_id, expected=target)
        except RemoteError as exc:
            message = ('远端渠道身份已改变，请同步核实后再归档' if exc.category == 'identity_mismatch'
                       else '无法读取并确认所有远端渠道已停用，请同步核实后再归档')
            raise HTTPException(409, message) from None
        status = remote_channel_state(raw_status)['status']
        if status != 'disabled':
            message = ('存在仍启用的远端渠道，请先停用并同步状态后再归档' if status == 'enabled'
                       else '远端渠道状态未知，不能归档；请先同步核实')
            raise HTTPException(409, message)
        checks.append({'distribution_id': dist.id, 'site_id': dist.site_id, 'remote_id': dist.remote_id,
                       'status': raw_status, 'checked_at': utcnow().isoformat()})
    for dist, check in zip(distributions, checks, strict=True):
        dist.status = 'disabled'
        dist.remote_snapshot = {**dist.remote_snapshot, 'status': check['status']}
    return checks
