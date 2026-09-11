"""Observed remote-channel states for already-authorized settlement groups."""
from types import SimpleNamespace

from sqlalchemy import select

from .distribution_status import observed_remote_state
from .distribution_tombstones import locally_deleted
from .models_channels import Distribution


def group_states(db, channel_ids, *, lock=False):
    """Callers authorize every channel first; only remote-state metadata is read."""
    states = {channel_id: [] for channel_id in channel_ids}
    if channel_ids:
        query = select(Distribution.channel_id, Distribution.remote_id, Distribution.status,
            Distribution.remote_snapshot['id'].label('snapshot_id'),
            Distribution.remote_snapshot['status'].label('snapshot_status'),
            Distribution.remote_snapshot['_local_deletion'].label('local_deletion'))
        query = query.where(Distribution.channel_id.in_(channel_ids))
        if lock:
            query = query.order_by(Distribution.id).with_for_update(read=True)
        rows = db.execute(query)
        for row in rows:
            dist = SimpleNamespace(remote_id=row.remote_id, status=row.status, remote_snapshot={
                'id': row.snapshot_id, 'status': row.snapshot_status, '_local_deletion': row.local_deletion})
            if dist.status == 'deleted' or locally_deleted(dist):
                continue
            if not dist.remote_id:
                # An uncertain create may already exist remotely; display its
                # state as unknown until a target has been confirmed.
                if dist.status in ('running', 'needs_review', 'created_pending_verification'):
                    states[row.channel_id].append('unavailable')
                continue
            # Incomplete operations and missing targets cannot confirm a retained snapshot.
            # Keep them visible as unknown targets rather than presenting an empty group.
            if dist.status not in ('enabled', 'disabled', 'unavailable'):
                states[row.channel_id].append('unavailable')
                continue
            observed = observed_remote_state(dist)
            status = observed['remote_status']
            if status == 'disabled':
                status = observed['remote_disable_reason'] + '_disabled'
            states[row.channel_id].append(status)
    result = {}
    for channel_id, observed in states.items():
        automatic_count = observed.count('automatic_disabled')
        if automatic_count:
            status, reason = 'automatic_disabled', '已有远端渠道自动禁用'
        elif not observed:
            status, reason = 'no_channels', '没有可确认状态的远端渠道'
        elif len(set(observed)) > 1:
            status, reason = 'mixed', '远端渠道状态不一致'
        elif observed[0] == 'enabled':
            status, reason = 'enabled', '远端渠道正在启用'
        elif observed[0] == 'manual_disabled':
            status, reason = 'manual_disabled', '全部远端渠道已手动禁用'
        else:
            status, reason = 'unavailable', '远端状态尚未确认'
        result[channel_id] = {'status': status, 'automatic_disabled_count': automatic_count,
                              'total': len(observed), 'reason': reason}
    return result
