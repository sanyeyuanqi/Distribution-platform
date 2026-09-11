"""Shared channel status semantics of the reviewed Silicon, Colin and NewAPI interfaces."""
from .distribution_tombstones import locally_deleted


def remote_channel_state(value):
    state = {'status': 'unavailable', 'disable_reason': None}
    if type(value) is int:
        if value == 1:
            state['status'] = 'enabled'
        elif value in (2, 3):
            state.update(status='disabled', disable_reason='manual' if value == 2 else 'automatic')
    return state


def distribution_remote_state(dist):
    """Normalize stored observations without replacing an unresolved local operation."""
    snapshot = getattr(dist, 'remote_snapshot', None)
    if (getattr(dist, 'remote_id', None) and dist.status in ('enabled', 'disabled', 'unavailable')
            and isinstance(snapshot, dict) and 'status' in snapshot and not locally_deleted(dist)):
        return remote_channel_state(snapshot['status'])
    return {'status': dist.status, 'disable_reason': None}
