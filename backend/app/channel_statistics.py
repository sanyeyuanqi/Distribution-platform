"""Local channel usage projection from immutable facts, with bounded SQL queries."""
from decimal import Decimal

from sqlalchemy import and_, case, func, select

from .models_billing import UsageFact


def empty_usage():
    return {'usage_by_unit': {}, 'verified_usage_by_unit': {}, 'raw_usage_by_unit': {},
            'fact_count': 0, 'unverified_count': 0, 'has_verified_amount': False,
            'source_cutoff': None, 'earliest_source_at': None}


def add_amount(target, unit, value):
    if value is not None:
        target[unit] = str(Decimal(target.get(unit, '0')) + value)


def distribution_usage(db, channels):
    """Aggregate all immutable facts for authorized channels in one database query.

    Units stay independent, including raw units with no configured conversion.
    Coverage means at least one verified fact, not continuous/current coverage.
    """
    if not channels:
        return {}
    qualified = and_(UsageFact.verified.is_(True), UsageFact.amount.is_not(None))
    query = select(UsageFact.distribution_id, UsageFact.unit, UsageFact.raw_unit,
                   func.sum(UsageFact.amount), func.sum(UsageFact.raw_amount),
                   func.sum(case((qualified, UsageFact.amount), else_=None)),
                   func.count(UsageFact.id),
                   func.sum(case((UsageFact.verified.is_(False), 1), else_=0)),
                   func.sum(case((qualified, 1), else_=0)),
                   func.max(func.coalesce(UsageFact.period_end, UsageFact.occurred_at)),
                   func.min(UsageFact.occurred_at))\
        .where(UsageFact.channel_id.in_([c.id for c in channels]),
               UsageFact.owner_id.in_({c.owner_id for c in channels}))\
        .group_by(UsageFact.distribution_id, UsageFact.unit, UsageFact.raw_unit)
    result = {}
    for distribution_id, unit, raw_unit, amount, raw, verified, count, unverified_count, covered, cutoff, earliest in db.execute(query):
        row = result.setdefault(distribution_id, empty_usage())
        add_amount(row['usage_by_unit'], unit, amount)
        add_amount(row['verified_usage_by_unit'], unit, verified)
        add_amount(row['raw_usage_by_unit'], raw_unit, raw)
        row['fact_count'] += count
        row['unverified_count'] += unverified_count
        row['has_verified_amount'] = row['has_verified_amount'] or covered > 0
        row['source_cutoff'] = max(row['source_cutoff'], cutoff) if row['source_cutoff'] else cutoff
        row['earliest_source_at'] = min(row['earliest_source_at'], earliest) if row['earliest_source_at'] else earliest
    return result


def sum_distributions(distributions, usage_map):
    result = empty_usage()
    covered = 0
    for dist in distributions:
        row = usage_map.get(dist.id)
        if not row:
            continue
        for name in ('usage_by_unit', 'verified_usage_by_unit', 'raw_usage_by_unit'):
            for unit, value in row[name].items():
                add_amount(result[name], unit, Decimal(value))
        result['fact_count'] += row['fact_count']
        result['unverified_count'] += row['unverified_count']
        covered += bool(row['has_verified_amount'])
        if row['source_cutoff']:
            result['source_cutoff'] = max(result['source_cutoff'], row['source_cutoff']) if result['source_cutoff'] else row['source_cutoff']
        if row['earliest_source_at']:
            result['earliest_source_at'] = min(result['earliest_source_at'], row['earliest_source_at']) if result['earliest_source_at'] else row['earliest_source_at']
    result.pop('has_verified_amount')
    result['coverage'] = {'covered': covered, 'total': len(distributions)}
    result['coverage_note'] = '已核实来源覆盖数；不代表截至当前时刻的连续统计完整性'
    result['data_status'] = ('missing' if result['fact_count'] == 0 else
                             'partial' if covered < len(distributions) or result['unverified_count'] else 'verified_sources')
    return result
