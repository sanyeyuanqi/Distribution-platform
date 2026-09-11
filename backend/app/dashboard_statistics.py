"""Bounded dashboard summaries over immutable historical usage facts."""
from datetime import datetime, time, timedelta

from sqlalchemy import Date, cast, func, select

from .billing import ZERO
from .models import Category
from .models_billing import UsageFact


def dashboard_usage_summary(db, owners, channel_counts, *, now):
    """Aggregate facts in SQL; return at most category/unit and 14-day buckets.

    Historical facts keep their original owner/category scope even when their
    channel has since moved. Current remote counters are handled separately.
    """
    owner_scope = UsageFact.owner_id.in_(owners)
    verified = UsageFact.verified.is_(True) & UsageFact.amount.is_not(None)
    buckets = db.execute(select(
        UsageFact.category_id,
        UsageFact.unit,
        func.sum(UsageFact.amount).label('known_amount'),
        func.sum(UsageFact.amount).filter(verified).label('verified_amount'),
        func.count().label('fact_count'),
        func.count().filter(UsageFact.verified.is_(False)).label('unverified_count'),
        func.max(UsageFact.occurred_at).label('source_cutoff'),
    ).where(owner_scope).group_by(UsageFact.category_id, UsageFact.unit)).all()

    known_totals, verified_totals, category_totals = {}, {}, {}
    fact_count = unverified_count = 0
    source_cutoff = None
    for bucket in buckets:
        category = category_totals.setdefault(bucket.category_id, {})
        if bucket.known_amount is not None:
            known_totals[bucket.unit] = known_totals.get(bucket.unit, ZERO) + bucket.known_amount
        if bucket.verified_amount is not None:
            verified_totals[bucket.unit] = verified_totals.get(bucket.unit, ZERO) + bucket.verified_amount
            category[bucket.unit] = bucket.verified_amount
        fact_count += bucket.fact_count
        unverified_count += bucket.unverified_count
        if source_cutoff is None or bucket.source_cutoff > source_cutoff:
            source_cutoff = bucket.source_cutoff

    covered = db.scalar(select(func.count(func.distinct(UsageFact.distribution_id)))
                        .where(owner_scope, verified))
    category_ids = channel_counts.keys() | category_totals.keys()
    names = dict(db.execute(select(Category.id, Category.name)
                            .where(Category.id.in_(category_ids))).all()) if category_ids else {}
    categories = [{'category_id': category_id, 'category_name': names[category_id],
        'channels': channel_counts.get(category_id, 0),
        'totals_by_unit': category_totals.get(category_id, {})}
        for category_id in sorted(category_ids)]

    offset = timedelta(hours=8)
    today = (now + offset).date()
    days = [today - timedelta(days=day) for day in reversed(range(14))]
    start = datetime.combine(days[0], time.min) - offset
    end = datetime.combine(today + timedelta(days=1), time.min) - offset
    local_day = cast(UsageFact.occurred_at + offset, Date)
    daily = db.execute(select(local_day.label('day'), UsageFact.unit,
        func.sum(UsageFact.amount).label('amount')).where(owner_scope, verified,
            UsageFact.occurred_at >= start, UsageFact.occurred_at < end)
        .group_by(local_day, UsageFact.unit)).all()
    daily_totals = {day: {} for day in days}
    for bucket in daily:
        daily_totals[bucket.day][bucket.unit] = bucket.amount

    return {'usage_by_unit': verified_totals, 'known_usage_by_unit': known_totals,
        'categories': categories,
        'trend': [{'date': str(day), 'totals_by_unit': daily_totals[day]} for day in days],
        'unverified_count': unverified_count, 'source_cutoff': source_cutoff,
        'fact_count': fact_count, 'covered': covered}
