"""A local removal never asserts that an upstream channel was deleted."""


def local_deletion(dist):
    if dist is None:
        return None
    marker = (getattr(dist, 'remote_snapshot', None) or {}).get('_local_deletion')
    return marker if isinstance(marker, dict) and marker.get('remote_confirmed') is False else None


def locally_deleted(dist):
    return local_deletion(dist) is not None
