import csv
import io
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import audit, get_current_user, require_roles, scope_owner_ids
from ..billing import ZERO, naive, record, serial
from ..billing_services import frozen_service_variant
from ..channel_services import filter_service_variant
from ..dashboard_statistics import dashboard_usage_summary
from ..db import get_db, utcnow
from ..model_capacity_gaps import platform_model_gaps
from ..models import Category, CredentialFormat, Site, User
from ..models_billing import UsageFact
from ..models_channels import Channel, Distribution, Task, UploadGroup
from ..remote_usage_totals import manual_usage_evidence, remote_usage_total

router = APIRouter(tags=['statistics'])
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]


def usage_rows(db, actor, scope='team', owner_id=None, category_id=None, start=None, end=None,
               *, variant=None, archived=None):
    owners = scope_owner_ids(db, actor, scope, owner_id)
    query = select(UsageFact).where(UsageFact.owner_id.in_(owners))
    if category_id:
        query = query.where(UsageFact.category_id == category_id)
    if variant is not None or archived is not None:
        channel_query = select(Channel).where(Channel.owner_id.in_(owners))
        if category_id:
            channel_query = channel_query.where(Channel.category_id == category_id)
        # Explicit 'all' still uses the authorized current-channel scope above;
        # omitting the parameter preserves the legacy historical export scope.
        if archived is not None and archived != 'all':
            channel_query = channel_query.where(Channel.archived.is_(archived))
        channel_query = filter_service_variant(db, channel_query, variant)
        query = query.join(Channel, Channel.id == UsageFact.channel_id).where(
            UsageFact.channel_id.in_(channel_query.with_only_columns(Channel.id)),
            UsageFact.owner_id == Channel.owner_id)
    if start:
        query = query.where(UsageFact.occurred_at >= naive(start))
    if end:
        query = query.where(UsageFact.occurred_at < naive(end))
    return list(db.scalars(query.order_by(UsageFact.occurred_at.desc(), UsageFact.id)))


def totals(rows):
    values = {}
    for row in rows:
        if row.amount is not None:
            values[row.unit] = values.get(row.unit, ZERO) + row.amount
    return serial(values)


@router.get('/usage')
def usage(db: DB, actor: Actor, scope: str = 'team', owner_id: str | None = None,
          category_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
          page: int = 1, page_size: int = 50):
    rows = usage_rows(db, actor, scope, owner_id, category_id, start, end)
    if page < 1 or page_size not in range(1, 201):
        raise HTTPException(422, '分页参数不合法')
    return {'items': [record(x, ('evidence',)) for x in rows[(page-1)*page_size:page*page_size]],
        'total': len(rows), 'totals_by_unit': totals(rows), 'timezone': 'Asia/Shanghai',
        'updated_at': serial(utcnow()), 'unverified_count': sum(not f.verified for f in rows)}


@router.get('/usage/export')
def usage_export(db: DB, actor: Actor, owner_id: str | None = None,
                 category_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
                 variant: str | None = None, archived: bool | Literal['all'] | None = None):
    rows = usage_rows(db, actor, owner_id=owner_id, category_id=category_id, start=start, end=end,
                      variant=variant, archived=archived)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['消耗记录编号', '站点编号', '渠道编号', '归属用户编号', '分类编号', '消耗发生时间（UTC）',
        '原始消耗', '原始单位', '计价基数', '计价单位', '核实状态', '换算规则版本',
        '来源类型', '统计时区', '导出时间（UTC）'])
    source_labels = {'verified_import': '手动导入', 'seller_log': '卖家日志', 'exact_interval': '精确区间'}
    exported = serial(utcnow())
    def safe(value):
        value = '' if value is None else str(value)
        return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
    for row in rows:
        writer.writerow([safe(x) for x in [row.id, row.site_id, row.channel_id, row.owner_id, row.category_id,
            serial(row.occurred_at), row.raw_amount, row.raw_unit, row.amount, row.unit,
            '已核实' if row.verified else '未核实', row.conversion_version,
            source_labels.get(row.source_kind, row.source_kind), 'Asia/Shanghai', exported]])
    audit(db, actor, 'usage.export', 'usage', owner_id or actor.id, {'count': len(rows)})
    db.commit()
    return Response('\ufeff' + buffer.getvalue(), media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="usage.csv"', 'Cache-Control': 'no-store'})


@router.get('/dashboard')
def dashboard(db: DB, actor: Actor, scope: str = 'team', owner_id: str | None = None):
    owners = scope_owner_ids(db, actor, scope, owner_id)
    now = utcnow()
    channel_counts = dict(db.execute(select(Channel.category_id, func.count())
        .where(Channel.owner_id.in_(owners)).group_by(Channel.category_id)).all())
    channel_ids = select(Channel.id).where(Channel.owner_id.in_(owners))
    distributions = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Distribution.id, Distribution.site_id, Distribution.remote_id, Distribution.status,
        Distribution.last_sync_at, Distribution.remote_snapshot)
        .where(Distribution.channel_id.in_(channel_ids)))]
    # Sum current counters once across the entire authorized scope, rather than
    # adding user/team totals (which would count child accounts twice).
    sites = {row.id: SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Site.id, Site.adapter, Site.base_url, Site.verified_at, Site.capabilities)
        .where(Site.id.in_({dist.site_id for dist in distributions})))} if distributions else {}
    current_usage = remote_usage_total(distributions, sites,
        evidence=manual_usage_evidence(db, distributions))
    if current_usage['total'] == 0:
        current_usage['amount'] = '0'
    history = dashboard_usage_summary(db, owners, channel_counts, now=now)
    fact_count, covered = history.pop('fact_count'), history.pop('covered')
    groups = db.scalar(select(func.count()).select_from(UploadGroup).where(UploadGroup.owner_id.in_(owners)))
    # Feature permission also applies to dashboard summaries, independently of ownership.
    recent = []
    if actor.role in ('superadmin', 'user'):
        task_columns = [column for column in Task.__table__.columns
                        if column.name not in ('payload', 'config_snapshot', 'snapshot')]
        recent = [dict(row._mapping) for row in db.execute(select(*task_columns)
            .where(Task.owner_id.in_(owners)).order_by(Task.created_at.desc()).limit(5))]
    return serial({'local_channels': sum(channel_counts.values()), 'remote_channels': sum(d.remote_id is not None for d in distributions),
        'groups': groups, 'users': len(owners), **history,
        'remote_usage_total': current_usage,
        'coverage': {'covered': covered, 'total': len(distributions)}, 'recent_tasks': recent,
        'updated_at': now, 'scope': scope,
        'coverage_note': '已核实来源覆盖数；不代表截至当前时刻的连续统计完整性',
        'data_status': 'partial' if covered < len(distributions) else ('empty' if not fact_count else 'verified_import_only')})


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


@router.get('/model-gaps')
def model_gaps(db: DB, actor: Actor):
    # This endpoint intentionally exposes platform aggregates, never ownership
    # or channel details. Personal/team scope belongs to other statistics APIs.
    return platform_model_gaps(db)


class FactIn(Strict):
    distribution_id: str
    source_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    period_end: datetime | None = None
    raw_amount: Decimal = Field(ge=0, max_digits=28, decimal_places=8)
    raw_unit: str = Field(min_length=1, max_length=40)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=28, decimal_places=8)
    unit: str = Field(default='USD', min_length=1, max_length=40)
    conversion_version: str = Field(min_length=1, max_length=120)
    verified: bool = False
    source_kind: str = Field(default='verified_import', pattern='^(verified_import|seller_log|exact_interval)$')
    evidence: str = Field(min_length=5, max_length=4000)


class ImportIn(Strict):
    items: list[FactIn] = Field(min_length=1, max_length=1000)


@router.post('/usage/import', dependencies=[Depends(require_roles('superadmin'))])
def import_facts(body: ImportIn, db: DB, actor: Actor):
    """Explicit evidence-backed import; never infer Silicon consumption from personal logs."""
    result = []
    for item in body.items:
        distribution = db.get(Distribution, item.distribution_id)
        if not distribution or not distribution.remote_id:
            raise HTTPException(422, '消耗来源必须关联已确认的远端渠道')
        channel = db.get(Channel, distribution.channel_id)
        owner = db.get(User, channel.owner_id)
        occurred = naive(item.occurred_at)
        valid_from = max(channel.created_at, distribution.created_at, distribution.adopted_at or channel.created_at)
        if occurred < valid_from or occurred > utcnow():
            raise HTTPException(422, '消耗发生时间不在渠道分发或接管后的有效范围内')
        if item.period_end and naive(item.period_end) <= occurred:
            raise HTTPException(422, '增量区间结束必须晚于开始')
        if item.period_end and naive(item.period_end) > utcnow():
            raise HTTPException(422, '增量区间不能延伸到未来')
        if item.verified and item.amount is None:
            raise HTTPException(422, '已核实事实必须提供计价金额及单位换算依据')
        previous = db.scalar(select(UsageFact).where(UsageFact.site_id == distribution.site_id,
            UsageFact.distribution_id == distribution.id, UsageFact.source_id == item.source_id))
        if previous:
            immutable = (previous.raw_amount, previous.raw_unit, previous.amount, previous.unit, previous.occurred_at,
                previous.conversion_version, previous.verified, previous.period_end)
            requested = (item.raw_amount, item.raw_unit, item.amount, item.unit, occurred,
                item.conversion_version, item.verified, naive(item.period_end))
            if immutable != requested:
                raise HTTPException(409, '源明细已经入库且不可覆盖，请核对原始记录')
            result.append({'id': previous.id, 'duplicate': True})
            continue
        row = UsageFact(**item.model_dump(exclude={'occurred_at', 'period_end'}), site_id=distribution.site_id,
            channel_id=channel.id, owner_id=owner.id, admin_id=owner.id if owner.role == 'admin' else owner.parent_id,
            category_id=channel.category_id, occurred_at=occurred, period_end=naive(item.period_end),
            service_variant=frozen_service_variant(db.get(Category, channel.category_id),
                                                   db.get(CredentialFormat, channel.format_id)))
        db.add(row)
        db.flush()
        result.append({'id': row.id, 'duplicate': False})
    audit(db, actor, 'usage.import', 'usage', actor.id, {'count': len(result)})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, '同时导入了相同源明细，请重新查询结果') from exc
    return {'items': result, 'total': len(result)}
