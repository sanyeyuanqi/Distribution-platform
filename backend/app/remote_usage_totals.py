"""Read-only USD totals from current remote observations, never settlement facts."""
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import select

from .adapters.channel_observation import (
    extract_usage,
    normalize_usage_observation,
    site_usage_conversion,
)
from .distribution_tombstones import locally_deleted
from .models_channels import TaskItem


def _usage_sync(dist):
    monitoring = (dist.remote_snapshot or {}).get('_monitoring')
    value = monitoring.get('usage_sync') if isinstance(monitoring, dict) else None
    return value if isinstance(value, dict) else {}


def manual_usage_evidence(db, distributions):
    """One metadata query for an entire page; no per-channel/task lookups."""
    task_ids = {value for dist in distributions if isinstance(value := _usage_sync(dist).get('task_id'), str)}
    if not task_ids:
        return {}
    rows = db.execute(select(TaskItem.distribution_id, TaskItem.task_id, TaskItem.updated_at,
                             TaskItem.snapshot['operation_target'].label('target'),
                             TaskItem.snapshot['operation_result'].label('result')).where(
        TaskItem.operation == 'sync_usage', TaskItem.distribution_id.in_([dist.id for dist in distributions]),
        TaskItem.task_id.in_(task_ids)))
    return {(row.distribution_id, row.task_id): dict(row._mapping) for row in rows}


def _time(value, *, database=False):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        if not database:
            return None
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _amount(usage):
    if usage.get('conversion_status') != 'available' or usage.get('used_amount_unit') != 'USD':
        return None
    value = usage.get('used_amount')
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _observed_amount(dist, site, evidence):
    snapshot = dist.remote_snapshot or {}
    if (dist.status not in ('enabled', 'disabled', 'unavailable')
            or type(snapshot.get('id')) not in (int, str) or str(snapshot['id']) != str(dist.remote_id)
            or type(snapshot.get('status')) is not int or snapshot['status'] not in (1, 2, 3)):
        return None
    if 'used_quota' in snapshot:
        return _amount(extract_usage(snapshot, conversion=site_usage_conversion(site)))
    observation = _usage_sync(dist)
    usage = observation.get('remote_usage')
    synced = _time(observation.get('synced_at'))
    linked = _time(getattr(dist, 'last_sync_at', None), database=True)
    if not isinstance(usage, dict) or synced is None or linked is None:
        return None
    # Legacy observations have no remote ID binding. A site sync writes the
    # linked snapshot/time first and the usage observation afterwards. Later
    # record_remote calls conservatively invalidate that timing evidence.
    if synced < linked:
        # Manual sync captures its result just before updating last_sync_at.
        # Its immutable target/result and completion timestamp prove this exact
        # observation; no arbitrary time tolerance or new FX ratio is used.
        proof = evidence.get((dist.id, observation.get('task_id'))) or {}
        target, result = proof.get('target'), proof.get('result')
        completed = _time(proof.get('updated_at'), database=True)
        if (not isinstance(target, dict) or type(target.get('id')) not in (int, str)
                or str(target['id']) != str(dist.remote_id) or not isinstance(result, dict)
                or result.get('synced_at') != observation.get('synced_at') or result.get('remote_usage') != usage
                or completed is None or linked > completed):
            return None
    # Keep the observation's own reviewed conversion. Missing/invalid historical
    # conversion stays unknown instead of being rewritten with today's ratio.
    return _amount(normalize_usage_observation(usage))


def remote_usage_total(distributions, sites, *, evidence=None):
    by_target = {}
    for dist in distributions:
        if not dist.remote_id or dist.status == 'deleted' or locally_deleted(dist):
            continue
        key = (dist.site_id, str(dist.remote_id))
        by_target.setdefault(key, []).append(_observed_amount(dist, sites.get(dist.site_id), evidence or {}))
    amounts = []
    for values in by_target.values():
        # Duplicate projections count once. Conflicting or missing evidence for
        # the same target is unknown, rather than selecting an arbitrary amount.
        if values[0] is not None and all(value == values[0] for value in values):
            amounts.append(values[0])
    amount = None
    if amounts:
        integer = max(1, max(value.adjusted() + 1 for value in amounts))
        fraction = max(0, max(-value.as_tuple().exponent for value in amounts))
        with localcontext() as context:
            context.prec = max(78, integer + fraction + len(str(len(amounts))) + 2)
            amount = format(sum(amounts, Decimal(0)), 'f')
        if '.' in amount:
            amount = amount.rstrip('0').rstrip('.')
    return {'amount': amount, 'unit': 'USD', 'covered': len(amounts), 'total': len(by_target)}
