"""Immutable remote-counter settlement orders; no transfers or legacy bill writes."""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import String, cast, func, select

from .adapters.channel_observation import (
    extract_usage,
    normalize_usage_observation,
    site_usage_conversion,
)
from .auth import audit, scope_owner_ids
from .billing import applicable_discount, digest, serial
from .channel_services import service_details
from .db import uid, utcnow
from .deletion_locks import lock_billing_snapshot
from .distribution_tombstones import locally_deleted
from .group_settlements import _channel_rows, _parties
from .models import Category, CredentialFormat, Site, User
from .models_billing import DiscountVersion, SettlementOrder, SettlementOrderLine
from .models_channels import Channel, Distribution, UploadGroup
from .remote_usage_totals import manual_usage_evidence, remote_usage_total
from .settlement_group_state import group_states

EIGHT = Decimal('0.00000001')


def _text(value):
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def _json(value):
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, datetime):
        return serial(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _sum(values):
    values = list(values)
    if not values:
        return Decimal(0)
    with localcontext() as context:
        context.prec = max(120, max(value.adjusted() + 1 for value in values)
                           + max(0, max(-value.as_tuple().exponent for value in values)) + len(str(len(values))) + 10)
        return sum(values, Decimal(0))


def _payment(usage, percent):
    with localcontext() as context:
        context.prec = max(120, len(usage.as_tuple().digits) + abs(usage.as_tuple().exponent) + 30)
        return (usage * percent / Decimal(100)).quantize(EIGHT, rounding=ROUND_HALF_UP)


def _identity(user):
    return {name: getattr(user, name) for name in ('id', 'username', 'nickname', 'role', 'parent_id')}


def _ids(channel_ids):
    if (not isinstance(channel_ids, list) or not 1 <= len(channel_ids) <= 100
            or any(not isinstance(value, str) or not value or len(value) > 36 for value in channel_ids)):
        raise HTTPException(422, '请选择 1–100 个有效渠道分组')
    return sorted(set(channel_ids))


def _authorize(db, actor, channel_ids, *, write, allow_unresolved=False):
    ids, channels = _channel_rows(db, actor, _ids(channel_ids))
    users = {user.id: user for user in db.scalars(select(User).where(
        User.id.in_({row.owner_id for row in channels.values()})).execution_options(populate_existing=True))}
    parents = {user.parent_id for user in users.values() if user.parent_id}
    users.update({user.id: user for user in db.scalars(select(User).where(User.id.in_(parents))
                                                    .execution_options(populate_existing=True))})
    resolved, by_owner = {}, {}
    for channel_id in ids:
        owner_id = channels[channel_id].owner_id
        if owner_id not in by_owner:
            by_owner[owner_id] = _parties(db, actor, users[owner_id], users)
        parties = by_owner[owner_id]
        if parties is None and not allow_unresolved:
            raise HTTPException(409, '所选分组没有合法的结算关系')
        if write and parties and parties[0].id != actor.id:
            raise HTTPException(404, '分组不在当前账号的付款范围内')
        resolved[channel_id] = parties
    return ids, channels, resolved


def _metadata(db, ids, *, lock=False):
    query = (select(Channel.id, Channel.display_id, Channel.owner_id,
        Channel.group_id, Channel.category_id, Channel.models, UploadGroup.tag.label('group_tag'),
        User.nickname.label('owner_name'), User.username.label('owner_username'),
        Category.name.label('category_name'), Category.family, CredentialFormat.schema_config)
        .join(UploadGroup, UploadGroup.id == Channel.group_id).join(User, User.id == Channel.owner_id)
        .join(Category, Category.id == Channel.category_id)
        .join(CredentialFormat, CredentialFormat.id == Channel.format_id).where(Channel.id.in_(ids)).order_by(Channel.id))
    if lock:
        query = query.with_for_update(read=True, of=(UploadGroup, User, Category, CredentialFormat))
    return {row.id: row for row in db.execute(query)}


def _sources(db, ids, *, lock=False):
    distributions = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Distribution.id, Distribution.channel_id, Distribution.site_id, Distribution.remote_id,
        Distribution.status, Distribution.last_sync_at, Distribution.remote_snapshot)
        .where(Distribution.channel_id.in_(ids)).order_by(Distribution.id))]
    query = select(Site.id, Site.name, Site.adapter, Site.base_url, Site.verified_at, Site.capabilities).where(
        Site.id.in_({dist.site_id for dist in distributions})).order_by(Site.id)
    if lock:
        query = query.with_for_update(read=True)
    sites = {row.id: SimpleNamespace(**dict(row._mapping)) for row in db.execute(query)}
    evidence = manual_usage_evidence(db, distributions)
    grouped = {channel_id: [] for channel_id in ids}
    for dist in distributions:
        grouped[dist.channel_id].append(dist)
    return grouped, sites, evidence


def _history(db, payer, payee, layer):
    baselines, by_channel, order_ids = {}, {}, {}
    rows = db.execute(select(SettlementOrderLine.channel_id, SettlementOrderLine.site_amounts, SettlementOrder.id)
        .join(SettlementOrder, SettlementOrder.id == SettlementOrderLine.order_id)
        .where(SettlementOrder.payer_id == payer.id, SettlementOrder.payee_id == payee.id,
               SettlementOrder.layer == layer, SettlementOrder.status == 'settled')
        .order_by(SettlementOrder.created_at, SettlementOrder.id, SettlementOrderLine.id))
    for channel_id, sources, order_id in rows:
        order_ids.setdefault(channel_id, set()).add(order_id)
        target_map = by_channel.setdefault(channel_id, {})
        for source in sources:
            key = (source['site_id'], source['remote_id'])
            value = Decimal(source['cumulative_total'])
            # Counters may never move backwards. Max also protects carried inactive
            # targets from overwriting a newer observation on another group line.
            if key not in baselines or value > Decimal(baselines[key]['cumulative_total']):
                baselines[key] = source
            if key not in target_map or value >= Decimal(target_map[key]['cumulative_total']):
                target_map[key] = source
    return baselines, by_channel, order_ids


def _safe_source(target, site, evidence, previous):
    amount = remote_usage_total(target, {site.id: site}, evidence=evidence)['amount']
    if amount is None:
        raise HTTPException(409, '站点累计消耗尚未完整核实，请等待后台同步完成后再结算')
    current = Decimal(amount)
    prior = Decimal(previous['cumulative_total']) if previous else Decimal(0)
    if current < prior:
        raise HTTPException(409, '站点累计消耗低于已结算基线，可能发生计数重置，请先核查')
    proofs = []
    for dist in target:
        snapshot = dist.remote_snapshot or {}
        if 'used_quota' in snapshot:
            usage = extract_usage(snapshot, conversion=site_usage_conversion(site))
        else:
            monitoring = snapshot.get('_monitoring')
            observation = monitoring.get('usage_sync') if isinstance(monitoring, dict) else None
            raw_usage = observation.get('remote_usage') if isinstance(observation, dict) else None
            usage = normalize_usage_observation(raw_usage) or {}
        conversion = usage.get('conversion') or {}
        safe_conversion = {key: conversion[key] for key in (
            'source', 'quota_per_unit', 'base_unit', 'adapter_kind', 'verified_version', 'protocol_contract')
            if key in conversion}
        proofs.append((str(usage['used_quota']), safe_conversion, conversion.get('observed_at')))
    raw_quota, conversion, conversion_observed_at = proofs[0]
    if any((quota, basis) != (raw_quota, conversion) for quota, basis, _ in proofs):
        raise HTTPException(409, '同一远端目标的额度或换算依据不一致，请先核查')
    if previous and (previous.get('conversion') != conversion
                     or Decimal(raw_quota) < Decimal(previous['used_quota'])):
        raise HTTPException(409, '站点额度换算依据已变化或原始计数下降，请先核查，不能直接计为新增消耗')
    latest = max(target, key=lambda row: (row.last_sync_at is not None, row.last_sync_at, row.id))
    return {'site_id': site.id, 'site_name': site.name, 'remote_id': str(latest.remote_id),
            'distribution_ids': sorted(row.id for row in target), 'active': True,
            'cumulative_total': amount, 'previous_total': _text(prior),
            'usage_amount': _text(_sum((current, prior.copy_negate()))), 'observed_at': _json(latest.last_sync_at),
            'used_quota': raw_quota, 'conversion': conversion, 'conversion_observed_at': conversion_observed_at,
            'remote_states': sorted({row.remote_snapshot.get('status') for row in target})}


def _line(row, distributions, sites, evidence, baselines, previous, history, now):
    total = remote_usage_total(distributions, sites, evidence=evidence)
    if total['amount'] is None or not total['total'] or total['covered'] != total['total']:
        raise HTTPException(409, '分组站点累计消耗缺失或覆盖不完整，请等待后台同步完成后再结算')
    grouped = {}
    for dist in distributions:
        if dist.remote_id and dist.status != 'deleted' and not locally_deleted(dist):
            grouped.setdefault((dist.site_id, str(dist.remote_id)), []).append(dist)
    source_rows, changed = {}, False
    for key, target in grouped.items():
        site = sites.get(key[0])
        if site is None:
            raise HTTPException(409, '分组站点信息缺失，请先核查')
        source = _safe_source(target, site, evidence, baselines.get(key))
        source_rows[key] = source
        changed = changed or key not in baselines or Decimal(source['usage_amount']) > 0
    service = service_details(SimpleNamespace(name=row.category_name, family=row.family),
                              SimpleNamespace(schema_config=row.schema_config), row.models)
    discount = applicable_discount(history, now, service['variant'])
    percent = discount.percent if discount else Decimal(0)
    if not percent.is_finite() or not 0 <= percent <= 100:
        raise HTTPException(422, '当前分组折扣不合法，请先配置 0–100% 的折扣')
    usage = _sum(Decimal(source['usage_amount']) for source in source_rows.values())
    prior = _sum(Decimal(source['previous_total']) for source in source_rows.values())
    for key, source in previous.items():
        if key not in source_rows:
            source_rows[key] = {**source, 'active': False, 'previous_total': source['cumulative_total'], 'usage_amount': '0'}
    line = {key: getattr(row, key) for key in ('display_id', 'group_id', 'group_tag', 'owner_id', 'owner_username',
                                               'category_id', 'category_name')}
    line.update(channel_id=row.id, owner_name=row.owner_name or row.owner_username, **service,
                discount_id=discount.id if discount else None, discount_percent=_text(percent),
                usage_amount=_text(usage), cumulative_total=total['amount'], previous_total=_text(prior),
                payment_amount=_text(_payment(usage, percent)),
                site_amounts=[source_rows[key] for key in sorted(source_rows)])
    return line, changed, set(grouped)


def _hash_quote(quote):
    # Observation timestamps are retained in the order, but a clock-only sync
    # must not invalidate a quote whose identities, counters and rates agree.
    normalized = {key: value for key, value in quote.items() if key != 'snapshot_hash'}
    normalized['lines'] = [{**line, 'site_amounts': [
        {key: value for key, value in source.items() if key not in ('observed_at', 'conversion_observed_at')}
        for source in line['site_amounts']]}
        for line in quote['lines']]
    return digest(normalized)


def _discounts(db, payer, payee, layer, category_ids):
    histories = {category_id: [] for category_id in category_ids}
    for version in db.scalars(select(DiscountVersion).where(DiscountVersion.payer_id == payer.id,
            DiscountVersion.payee_id == payee.id, DiscountVersion.layer == layer,
            DiscountVersion.category_id.in_(histories))
            .order_by(DiscountVersion.effective_at, DiscountVersion.created_at, DiscountVersion.id)):
        histories[version.category_id].append(version)
    return histories


def _quote(db, actor, channel_ids, *, lock=False, write=True, allow_unchanged=False):
    ids, _, resolved = _authorize(db, actor, channel_ids, write=write)
    scopes = {(payer.id, payee.id, layer) for payer, payee, layer in resolved.values()}
    if len(scopes) != 1:
        raise HTTPException(422, '一次订单只能包含相同付款方、收款方和结算层级的分组')
    payer, payee, layer = resolved[ids[0]]
    if lock:
        if db.bind.dialect.name == 'postgresql':
            lock_billing_snapshot(db)
        db.execute(select(User.id).where(User.id == payee.id).with_for_update(key_share=True)).all()
        db.execute(select(Channel.id).where(Channel.id.in_(ids)).order_by(Channel.id).with_for_update()).all()
        _metadata(db, ids, lock=True)
        _, _, fresh = _authorize(db, actor, ids, write=write)
        if {(a.id, b.id, level) for a, b, level in fresh.values()} != scopes:
            raise HTTPException(409, '分组归属已变化，请重新预览')
        payer, payee, layer = fresh[ids[0]]
    # Observed enable/disable states are informational. On confirmation this
    # also locks distributions until their saved usage evidence is registered.
    states = group_states(db, ids, lock=lock)
    metadata = _metadata(db, ids)
    grouped, sites, evidence = _sources(db, ids, lock=lock)
    baselines, old_sources, order_ids = _history(db, payer, payee, layer)
    histories = _discounts(db, payer, payee, layer, {row.category_id for row in metadata.values()})
    lines, unchanged, seen = [], [], set()
    now = utcnow()
    for channel_id in ids:
        row = metadata[channel_id]
        line, changed, targets = _line(row, grouped[channel_id], sites, evidence, baselines,
            old_sources.get(channel_id, {}), histories[row.category_id], now)
        if seen & targets:
            raise HTTPException(409, '所选分组包含重复远端目标，请先核对渠道归属')
        seen.update(targets)
        if not changed:
            unchanged.append(channel_id)
        # Aggregate counters are live group projections, not order-line amounts.
        # Keep per-target evidence for subsequent incremental settlements.
        lines.append({key: value for key, value in line.items()
                      if key not in ('cumulative_total', 'previous_total')})
    if unchanged and not allow_unchanged:
        raise HTTPException(409, '部分所选分组没有新增消耗或新站点基线，同一数据不能重复生成订单')
    quote = {'payer_id': payer.id, 'payer_name': payer.nickname or payer.username,
             'payee_id': payee.id, 'payee_name': payee.nickname or payee.username, 'layer': layer,
             'identity_snapshot': {'actor': _identity(actor), 'payer': _identity(payer), 'payee': _identity(payee)},
             'usage_amount': _text(_sum(Decimal(line['usage_amount']) for line in lines)),
             'payment_amount': _text(_sum(Decimal(line['payment_amount']) for line in lines)),
             'pricing_unit': 'USD', 'payment_unit': 'USDT', 'line_count': len(lines), 'lines': lines}
    quote['snapshot_hash'] = _hash_quote(quote)
    return quote, unchanged, order_ids, states


def preview_order(db, actor, channel_ids):
    quote, _, _, _ = _quote(db, actor, channel_ids)
    return {key: value for key, value in quote.items() if key != 'identity_snapshot'}


def _header(order):
    fields = ('id', 'number', 'status', 'actor_id', 'actor_name', 'payer_id', 'payer_name', 'payee_id', 'payee_name',
              'layer', 'usage_amount', 'payment_amount', 'pricing_unit', 'payment_unit', 'line_count', 'created_at', 'snapshot_hash')
    result = _json({field: getattr(order, field) for field in fields})
    result['number'] = order.number.replace('-', '')
    return result


def order_detail(db, order):
    result = _header(order)
    result['lines'] = [_json({column.name: getattr(line, column.name) for column in line.__table__.columns})
                       for line in db.scalars(select(SettlementOrderLine).where(SettlementOrderLine.order_id == order.id)
                                              .order_by(SettlementOrderLine.channel_id))]
    return result


def create_order(db, actor, channel_ids, snapshot_hash, idempotency_key):
    ids = _ids(channel_ids)
    request_hash = digest({'channel_ids': ids, 'snapshot_hash': snapshot_hash})
    # Preserve the session's authorization while this transaction registers an
    # order; a concurrent disable/password/role change must take effect first.
    expected_version, expected_role = actor.session_version, actor.role
    locked = db.execute(select(User.active, User.archived, User.session_version, User.role)
        .where(User.id == actor.id).with_for_update(read=True)).one_or_none()
    if (not locked or not locked.active or locked.archived or locked.session_version != expected_version
            or locked.role != expected_role):
        raise HTTPException(401, '账号权限或会话已变化，请重新登录')
    if locked.role not in ('superadmin', 'admin'):
        raise HTTPException(403, '当前账号不能登记结算订单')

    def previous():
        existing = db.scalar(select(SettlementOrder).where(SettlementOrder.actor_id == actor.id,
                                                           SettlementOrder.idempotency_key == idempotency_key))
        if existing and existing.request_hash != request_hash:
            raise HTTPException(409, '幂等标识已用于不同的订单请求')
        return existing

    _authorize(db, actor, ids, write=True)
    existing = previous()
    if existing:
        return existing
    # Serialize requests for the real payer/payee scope before checking history.
    _, _, parties = _authorize(db, actor, ids, write=True)
    scopes = {(payer.id, payee.id, layer) for payer, payee, layer in parties.values()}
    if len(scopes) != 1:
        raise HTTPException(422, '一次订单只能包含相同付款方、收款方和结算层级的分组')
    payee = parties[ids[0]][1]
    db.execute(select(User.id).where(User.id == payee.id).with_for_update(key_share=True)).all()
    existing = previous()
    if existing:
        return existing
    quote, _, _, _ = _quote(db, actor, ids, lock=True)
    if quote['snapshot_hash'] != snapshot_hash:
        raise HTTPException(409, '分组身份、标签、折扣或累计消耗已变化，请重新预览')
    order_id = uid()
    order = SettlementOrder(id=order_id, number=f'SO{utcnow():%Y%m%d}{order_id[:13].replace("-", "").upper()}',
        actor_id=actor.id, actor_name=actor.nickname or actor.username,
        **{key: quote[key] for key in ('payer_id', 'payer_name', 'payee_id', 'payee_name', 'layer', 'identity_snapshot',
                                      'pricing_unit', 'payment_unit', 'line_count', 'snapshot_hash')},
        usage_amount=Decimal(quote['usage_amount']), payment_amount=Decimal(quote['payment_amount']),
        idempotency_key=idempotency_key, request_hash=request_hash)
    db.add(order)
    db.flush()
    for line in quote['lines']:
        values = {**line}
        for key in ('discount_percent', 'usage_amount', 'payment_amount'):
            values[key] = Decimal(values[key])
        db.add(SettlementOrderLine(order_id=order.id, **values))
    db.flush()
    audit(db, actor, 'settlement_order.create', 'settlement_order', order.id,
          {'number': order.number, 'line_count': order.line_count, 'payment_amount': quote['payment_amount']})
    return order


def get_order(db, actor, order_id):
    order = db.get(SettlementOrder, order_id)
    if order is None or (actor.role != 'superadmin' and actor.id not in (order.payer_id, order.payee_id)):
        raise HTTPException(404, '结算订单不存在')
    return order_detail(db, order)


def list_orders(db, actor, account_id=None, search='', offset=0, limit=50, *, payee_id=None):
    query = select(SettlementOrder)
    if actor.role != 'superadmin':
        query = query.where((SettlementOrder.payer_id == actor.id) | (SettlementOrder.payee_id == actor.id))
    if account_id:
        allowed = scope_owner_ids(db, actor)
        account = db.get(User, account_id)
        if (not account or account.id not in allowed or account.role == 'superadmin'
                or (actor.role == 'admin' and (account.role != 'user' or account.parent_id != actor.id))):
            raise HTTPException(404, '账号不在授权范围内')
        owners = [account.id]
        if account.role == 'admin':
            owners += list(db.scalars(select(User.id).where(User.parent_id == account.id, User.role == 'user')))
        query = query.where(SettlementOrder.id.in_(select(SettlementOrderLine.order_id).where(
            SettlementOrderLine.owner_id.in_(owners))))
    if payee_id:
        allowed = scope_owner_ids(db, actor)
        payee = db.get(User, payee_id)
        if (not payee or payee.id not in allowed or payee.role == 'superadmin'
                or (actor.role == 'admin' and (payee.role != 'user' or payee.parent_id != actor.id))):
            raise HTTPException(404, '账号不在授权范围内')
        # Match the frozen recipient, as in account order counts and amounts.
        query = query.where(SettlementOrder.payee_id == payee.id)
    if search.strip():
        term = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        number_match = SettlementOrder.number.ilike(term, escape='\\')
        if search.strip().replace('-', ''):
            # Old order numbers remain frozen in storage; either format is searchable.
            number_match |= func.replace(SettlementOrder.number, '-', '').ilike(term.replace('-', ''), escape='\\')
        matching = select(SettlementOrderLine.order_id).where(SettlementOrderLine.group_tag.ilike(term, escape='\\')
            | SettlementOrderLine.owner_username.ilike(term, escape='\\')
            | SettlementOrderLine.service_name.ilike(term, escape='\\')
            | cast(SettlementOrderLine.display_id, String).ilike(term, escape='\\'))
        query = query.where(number_match
            | SettlementOrder.payer_name.ilike(term, escape='\\') | SettlementOrder.payee_name.ilike(term, escape='\\')
            | SettlementOrder.id.in_(matching))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(SettlementOrder.created_at.desc(), SettlementOrder.id.desc()).offset(offset).limit(limit))
    return {'items': [_header(row) for row in rows], 'total': total}


def order_group_status(db, actor, channel_ids):
    # Resolve the complete batch first: one unauthorized ID rejects the request.
    ids, _, parties = _authorize(db, actor, channel_ids, write=False, allow_unresolved=True)
    states = group_states(db, ids)
    metadata = _metadata(db, ids)
    grouped, sites, evidence = _sources(db, ids)
    scopes = {}
    for channel_id, scope in parties.items():
        if scope:
            payer, payee, layer = scope
            scopes.setdefault((payer.id, payee.id, layer), []).append(channel_id)
    scoped_data = {}
    for key, selected in scopes.items():
        payer, payee, layer = parties[selected[0]]
        scoped_data[key] = (_history(db, payer, payee, layer),
            _discounts(db, payer, payee, layer, {metadata[value].category_id for value in selected}))
    now = utcnow()
    result = []
    for channel_id in ids:
        row = {'channel_id': channel_id, 'payer_id': None, 'payer_name': '', 'payee_id': None, 'payee_name': '',
               'layer': None, 'can_manage': False, 'can_settle': False, 'status': 'unavailable', 'reason': '',
               'discount_id': None, 'discount_percent': None, 'cumulative_total': None, 'previous_total': None,
               'usage_amount': None, 'payment_amount': None, 'order_ids': [], 'group_state': states[channel_id]}
        try:
            scope = parties[channel_id]
            if scope is None:
                raise HTTPException(409, '所选分组没有合法的结算关系')
            payer, payee, layer = scope
            row.update(payer_id=payer.id, payer_name=payer.nickname or payer.username,
                       payee_id=payee.id, payee_name=payee.nickname or payee.username, layer=layer,
                       can_manage=payer.id == actor.id)
            (baselines, old_sources, order_ids), histories = scoped_data[(payer.id, payee.id, layer)]
            row['order_ids'] = sorted(order_ids.get(channel_id, []))
            info = metadata[channel_id]
            service = service_details(SimpleNamespace(name=info.category_name, family=info.family),
                                      SimpleNamespace(schema_config=info.schema_config), info.models)
            discount = applicable_discount(histories[info.category_id], now, service['variant'])
            row.update(discount_id=discount.id if discount else None,
                       discount_percent=_text(discount.percent if discount else Decimal(0)))
            line, changed, _ = _line(info, grouped[channel_id], sites, evidence, baselines,
                old_sources.get(channel_id, {}), histories[info.category_id], now)
            row.update({key: line[key] for key in ('discount_id', 'discount_percent', 'cumulative_total',
                                                  'previous_total', 'usage_amount', 'payment_amount')})
            row['status'] = 'unsettled' if changed else 'settled'
            row['reason'] = '当前有可登记的新消耗或首次站点基线' if changed else '当前累计消耗已登记结算'
            row['can_settle'] = row['can_manage'] and changed
        except HTTPException as exc:
            detail = exc.detail
            row['reason'] = detail.get('message', str(detail)) if isinstance(detail, dict) else str(detail)
        result.append(row)
    return result
