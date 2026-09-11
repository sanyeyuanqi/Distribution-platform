"""Frozen incremental settlement orders use verified remote totals, not legacy billing facts."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.channel_purge import purge_distributions
from app.db import SessionLocal, engine, uid, utcnow
from app.default_discounts import ensure_all_default_discounts
from app.models import Site, User
from app.models_billing import (
    DiscountVersion,
    SettlementOrder,
    SettlementOrderLine,
    UsageFact,
)
from app.models_channels import Channel, Distribution, UploadGroup
from app.remote_usage_totals import remote_usage_total
from app.routers.billing import DiscountIn, set_discount
from app.settlement_orders import create_order, preview_order
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from test_channel_categories import service_case as _service_fixture
from test_remote_usage_totals import STAMP, VERSION, conversion

service_case = _service_fixture
ORDERS = '/api/settlement-orders'


def _observe(dist, quota, *, status=3):
    dist.remote_id = dist.remote_id or uid()
    dist.status = 'enabled' if status == 1 else 'disabled'
    dist.last_sync_at = STAMP.replace(tzinfo=None)
    dist.remote_snapshot = {'id': dist.remote_id, 'status': status, 'used_quota': quota,
                            'key': 'fixture-order-snapshot-secret'}


def _rate(db, users, case, percent='80', *, owner='user', payer='admin', layer='lower',
          variant='bedrock', effective_at=None):
    row = DiscountVersion(id=uid(), payer_id=users[payer].id, payee_id=users[owner].id, layer=layer,
        category_id=case.categories['AWS'].id, service_variant=variant, percent=Decimal(percent),
        effective_at=effective_at or utcnow()-timedelta(days=1))
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def order_case(db, users, service_case):
    ensure_all_default_discounts(db)
    rate = _rate(db, users, service_case)
    _rate(db, users, service_case, '90', owner='admin', payer='root', layer='upper')

    def make(*, quota=50_000_000, owner='user', status=3, code='newapi-33-aws-bedrock-v1'):
        channel, dist = service_case.channel('AWS', code, owner=owner)
        site = db.get(Site, dist.site_id)
        site.adapter = 'tcp-red-v1'
        site.verified_at = STAMP.replace(tzinfo=None)
        site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(500000)}
        _observe(dist, quota, status=status)
        db.flush()
        return channel, dist

    def target(channel, *, quota=5_000_000, status=3):
        first = db.scalar(select(Distribution).where(Distribution.channel_id == channel.id))
        dist = Distribution(id=uid(), channel_id=channel.id, site_id=first.site_id,
            partition_key=uid(), remote_name='order-fixture-extra-target')
        _observe(dist, quota, status=status)
        db.add(dist)
        db.flush()
        return dist

    pair = make()
    db.commit()
    return SimpleNamespace(pair=pair, make=make, target=target, rate=rate,
                           categories=service_case.categories, fact=service_case.fact)


def _usage_snapshot(db):
    models = (UsageFact,)
    return {model.__tablename__: {row.id: deepcopy({column.key: getattr(row, column.key)
            for column in model.__table__.columns}) for row in db.scalars(
                select(model).execution_options(populate_existing=True))}
            for model in models}


def _counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model))
                 for model in (SettlementOrder, SettlementOrderLine))


def _preview(api, channels):
    response = api.post(ORDERS+'/preview', json={'channel_ids': [row.id for row in channels]})
    assert response.status_code == 200, response.text
    quote = response.json()
    assert all('cumulative_total' not in line and 'previous_total' not in line for line in quote['lines'])
    return quote


def _create(api, channels, quote=None, *, key=None):
    quote = quote if quote is not None else _preview(api, channels)
    response = api.post(ORDERS, json={'channel_ids': [row.id for row in channels],
        'snapshot_hash': quote['snapshot_hash'], 'idempotency_key': key or uid()})
    assert response.status_code == 201, response.text
    return response.json()


def _detail(api, order):
    response = api.get(ORDERS+'/'+order['id'])
    assert response.status_code == 200, response.text
    detail = response.json()
    assert all('cumulative_total' not in line and 'previous_total' not in line for line in detail['lines'])
    return detail


def _line(quote, channel):
    return next(line for line in quote['lines'] if line['channel_id'] == channel.id)




def test_preview_is_read_only_and_new_order_uses_only_selected_remote_totals_and_current_rates(
        db, users, order_case, login):
    first, _ = order_case.pair
    second, _ = order_case.make(quota=12_500_000, status=2)
    unselected, _ = order_case.make(quota=999_000_000)
    db.commit()
    api = login('admin')
    before = _usage_snapshot(db)
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        quote = _preview(api, [second, first])
        assert _preview(api, [first, second]) == quote
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert _counts(db) == (0, 0) and _usage_snapshot(db) == before
    assert all(not rows for rows in before.values())
    assert quote['line_count'] == 2
    assert {line['channel_id'] for line in quote['lines']} == {first.id, second.id}
    assert unselected.id not in {line['channel_id'] for line in quote['lines']}
    assert (quote['payer_id'], quote['payee_id'], quote['layer']) == (
        users['admin'].id, users['user'].id, 'lower')
    assert (quote['pricing_unit'], quote['payment_unit']) == ('USD', 'USDT')
    assert Decimal(quote['usage_amount']) == 125 and Decimal(quote['payment_amount']) == 100
    assert sum(Decimal(line['payment_amount']) for line in quote['lines']) == 100
    line = _line(quote, first)
    assert line['variant'] == 'bedrock' and line['service_name'] == 'AWS Bedrock'
    assert line['discount_id'] == order_case.rate.id
    assert Decimal(line['discount_percent']) == 80
    assert Decimal(line['usage_amount']) == 100 and Decimal(line['payment_amount']) == 80
    order = _create(api, [first, second], quote)
    assert order['status'] == 'settled' and order['number'] and order['created_at']
    assert order['actor_id'] == users['admin'].id
    assert _counts(db) == (1, 2) and _usage_snapshot(db) == before


def test_order_detail_freezes_names_rate_conversion_consumption_and_payment(db, users, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')
    order = _create(api, [channel])
    original = _detail(api, order)
    _rate(db, users, order_case, '20', effective_at=utcnow()-timedelta(minutes=1))
    _observe(dist, 90_000_000)
    site = db.get(Site, dist.site_id)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(1_000_000)}
    db.get(UploadGroup, channel.group_id).tag = 'renamed-after-order'
    users['user'].nickname = 'Renamed after settlement'
    channel.archived = True
    db.commit()
    assert _detail(api, order) == original
    assert _detail(login('user'), order) == original
    line = _line(original, channel)
    assert Decimal(line['discount_percent']) == 80
    assert Decimal(line['usage_amount']) == 100 and Decimal(line['payment_amount']) == 80
    assert 'renamed-after-order' not in str(original)


def test_new_order_number_is_stored_and_returned_without_hyphens(db, order_case, login):
    order = _create(login('admin'), [order_case.pair[0]])
    assert order['number'].startswith('SO') and '-' not in order['number']
    assert db.scalar(select(SettlementOrder.number).where(
        SettlementOrder.id == order['id'])) == order['number']


def test_legacy_order_number_is_normalized_for_reads_and_search_without_rewriting_storage(
        db, order_case, login):
    api = login('admin')
    order = _create(api, [order_case.pair[0]])
    legacy_number = 'SO-20260911-A1B2C3D4E5F6'
    normalized_number = legacy_number.replace('-', '')
    db.get(SettlementOrder, order['id']).number = legacy_number
    db.commit()

    assert _detail(api, order)['number'] == normalized_number
    for search in ('', legacy_number, normalized_number, legacy_number.lower(), normalized_number.lower()):
        response = api.get(ORDERS, params={'search': search})
        assert response.status_code == 200, response.text
        listing = response.json()
        assert listing['total'] == 1
        assert [(item['id'], item['number']) for item in listing['items']] == [
            (order['id'], normalized_number)]
    for suffix in ('%', '_'):
        response = api.get(ORDERS, params={'search': normalized_number + suffix})
        assert response.status_code == 200, response.text
        assert response.json()['total'] == 0 and response.json()['items'] == []

    assert db.scalar(select(SettlementOrder.number).where(
        SettlementOrder.id == order['id'])) == legacy_number


def test_repeat_orders_use_per_target_increment_and_current_rate_without_repricing_history(
        db, users, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')
    first = _create(api, [channel])
    first_detail = _detail(api, first)
    _observe(dist, 62_500_000)
    order_case.target(channel, quota=5_000_000)
    _rate(db, users, order_case, '50', effective_at=utcnow()-timedelta(minutes=1))
    db.commit()
    quote = _preview(api, [channel])
    line = _line(quote, channel)
    assert Decimal(line['usage_amount']) == 35 and Decimal(line['payment_amount']) == Decimal('17.5')
    assert len(line['site_amounts']) == 2
    second = _create(api, [channel], quote)
    assert second['id'] != first['id'] and _counts(db) == (2, 2)
    assert _detail(api, first) == first_detail
    rejected = api.post(ORDERS+'/preview', json={'channel_ids': [channel.id]})
    assert rejected.status_code == 409, rejected.text
    assert _counts(db) == (2, 2)


def test_account_group_order_projection_is_read_only_and_tracks_only_new_consumption(
        db, users, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')

    def listing():
        before = (_counts(db), _usage_snapshot(db))
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().lower())

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            response = api.get('/api/settlements/groups', params={'account_id': users['user'].id})
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert response.status_code == 200, response.text
        assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
        assert (_counts(db), _usage_snapshot(db)) == before
        row = next(row for row in response.json()['items'] if row['id'] == channel.id)
        return row['settlement']

    first = listing()
    assert first['can_settle'] is True and first['status'] == 'unsettled'
    assert Decimal(first['usage_amount']) == 100 and Decimal(first['payment_amount']) == 80
    order = _create(api, [channel])
    settled = listing()
    assert settled['status'] == 'settled' and settled['can_settle'] is False
    assert settled['order_ids'] == [order['id']]
    assert Decimal(settled['usage_amount']) == Decimal(settled['payment_amount']) == 0
    _observe(dist, 60_000_000)
    db.commit()
    increased = listing()
    assert increased['status'] == 'unsettled' and increased['can_settle'] is True
    assert Decimal(increased['cumulative_total']) == 120 and Decimal(increased['previous_total']) == 100
    assert Decimal(increased['usage_amount']) == 20 and Decimal(increased['payment_amount']) == 16


@pytest.mark.parametrize('zero', ['default-rate', 'usage'])
def test_first_zero_order_is_valid_but_unchanged_counters_cannot_be_settled_again(
        db, order_case, login, zero):
    if zero == 'default-rate':
        channel, _ = order_case.make(code='newapi-14-aws-claude-v1')
    else:
        channel, _ = order_case.make(quota=0)
    db.commit()
    api = login('admin')
    quote = _preview(api, [channel])
    assert Decimal(quote['payment_amount']) == 0
    assert Decimal(quote['usage_amount']) == (100 if zero == 'default-rate' else 0)
    if zero == 'default-rate':
        assert Decimal(_line(quote, channel)['discount_percent']) == 0
    order = _create(api, [channel], quote)
    assert order['status'] == 'settled' and Decimal(order['payment_amount']) == 0
    again = api.post(ORDERS, json={'channel_ids': [channel.id], 'snapshot_hash': quote['snapshot_hash'],
        'idempotency_key': uid()})
    assert again.status_code == 409, again.text
    assert _counts(db) == (1, 1)


@pytest.mark.parametrize(('quota', 'ratio', 'rate', 'usage', 'payment'), [
    (1, 100_000_000, '50', '0.00000001', '0.00000001'),
    (51, 10_000_000_000, '80', '0.0000000051', '0'),
    (9_007_199_254_740_993, 1, '80', '9007199254740993', '7205759403792794.4'),
])
def test_current_fine_rate_wins_over_default_and_future_rate_and_rounding_is_half_up(
        db, users, order_case, login, quota, ratio, rate, usage, payment):
    channel, dist = order_case.pair
    _observe(dist, quota)
    site = db.get(Site, dist.site_id)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(ratio)}
    _rate(db, users, order_case, rate, effective_at=utcnow()-timedelta(minutes=2))
    _rate(db, users, order_case, '99', effective_at=utcnow()+timedelta(days=1))
    db.commit()
    api = login('admin')
    quote = _preview(api, [channel])
    assert Decimal(quote['usage_amount']) == Decimal(usage)
    assert Decimal(quote['payment_amount']) == Decimal(payment)
    assert Decimal(_line(quote, channel)['discount_percent']) == Decimal(rate)
    stored = _detail(api, _create(api, [channel], quote))
    assert Decimal(stored['usage_amount']) == Decimal(usage)
    assert Decimal(_line(stored, channel)['usage_amount']) == Decimal(usage)
    assert Decimal(stored['payment_amount']) == Decimal(payment)


@pytest.mark.parametrize('change', ['counter', 'rate', 'enabled', 'owner', 'metadata', 'partial', 'scope'])
def test_create_rechecks_quote_source_rate_remote_state_ownership_and_frozen_metadata(
        db, users, order_case, login, change):
    channel, dist = order_case.pair
    api = login('admin')
    quote = _preview(api, [channel])
    requested = [channel.id]
    if change == 'counter':
        _observe(dist, 55_000_000)
    elif change == 'rate':
        _rate(db, users, order_case, '20', effective_at=utcnow()-timedelta(minutes=1))
    elif change == 'enabled':
        _observe(dist, 50_000_000, status=1)
    elif change == 'owner':
        channel.owner_id = users['other_user'].id
    elif change == 'partial':
        extra = order_case.target(channel)
        extra.remote_snapshot = {'id': extra.remote_id, 'status': 3}
    elif change == 'scope':
        other, _ = order_case.make()
        requested.append(other.id)
    else:
        db.get(UploadGroup, channel.group_id).tag = 'changed-before-confirmation'
    db.commit()
    result = api.post(ORDERS, json={'channel_ids': requested, 'snapshot_hash': quote['snapshot_hash'],
        'idempotency_key': uid()})
    expected = (404,) if change == 'owner' else ((409, 422) if change == 'partial' else (409,))
    assert result.status_code in expected, result.text
    assert _counts(db) == (0, 0)
    if change == 'enabled':
        # A changed observed state requires a new frozen snapshot, but an
        # enabled channel can settle using that current snapshot immediately.
        order = _create(api, [channel])
        assert Decimal(order['usage_amount']) == 100 and Decimal(order['payment_amount']) == 80
        assert _counts(db) == (1, 1)


@pytest.mark.parametrize('invalid', ['missing-quota', 'partial', 'decreased'])
def test_unknown_partial_and_decreasing_counters_are_rejected_without_new_orders(db, order_case, login, invalid):
    channel, dist = order_case.pair
    api = login('admin')
    before = (0, 0)
    if invalid == 'decreased':
        _create(api, [channel])
        before = (1, 1)
        _observe(dist, 49_000_000)
    else:
        target = order_case.target(channel) if invalid == 'partial' else dist
        target.remote_snapshot = {'id': target.remote_id, 'status': 3}
    db.commit()
    response = api.post(ORDERS+'/preview', json={'channel_ids': [channel.id]})
    assert response.status_code in (409, 422), response.text
    assert _counts(db) == before


@pytest.mark.parametrize(('quota', 'converted'), [
    (50_000_000, '200'),
    (37_500_000, '150'),
], ids=['unchanged-counter', 'lower-counter'])
def test_changed_conversion_cannot_turn_unchanged_or_lower_counters_into_new_usage(
        db, order_case, login, quota, converted):
    channel, dist = order_case.pair
    api = login('admin')
    quote = _preview(api, [channel])
    assert Decimal(quote['usage_amount']) == 100
    order = _create(api, [channel], quote)
    original_order = _detail(api, order)
    original_legacy = _usage_snapshot(db)
    remote_id = dist.remote_id
    _observe(dist, quota)
    site = db.get(Site, dist.site_id)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(250000)}
    db.commit()
    assert dist.remote_id == remote_id
    current = remote_usage_total([dist], {site.id: site})
    assert Decimal(current['amount']) == Decimal(converted) > 100
    preview = api.post(ORDERS+'/preview', json={'channel_ids': [channel.id]})
    assert preview.status_code == 409, preview.text
    create = api.post(ORDERS, json={'channel_ids': [channel.id], 'snapshot_hash': quote['snapshot_hash'],
        'idempotency_key': uid()})
    assert create.status_code == 409, create.text
    assert _counts(db) == (1, 1)
    assert _detail(api, order) == original_order
    assert _usage_snapshot(db) == original_legacy


def test_idempotency_returns_one_frozen_order_and_rejects_different_scope_or_hash(db, order_case, login):
    first, _ = order_case.pair
    second, _ = order_case.make(quota=5_000_000)
    db.commit()
    api = login('admin')
    quote = _preview(api, [first])
    key = uid()
    order = _create(api, [first], quote, key=key)
    assert _create(api, [first], quote, key=key) == order
    for channels, snapshot_hash in (([second.id], quote['snapshot_hash']), ([first.id], 'f'*64)):
        result = api.post(ORDERS, json={'channel_ids': channels, 'snapshot_hash': snapshot_hash,
            'idempotency_key': key})
        assert result.status_code == 409, result.text
    assert _counts(db) == (1, 1)


def test_upper_lower_and_root_direct_user_have_independent_order_scopes(db, users, order_case, login):
    channel, _ = order_case.pair
    lower = _create(login('admin'), [channel])
    upper = _create(login('root'), [channel])
    assert (lower['payer_id'], lower['payee_id'], lower['layer']) == (
        users['admin'].id, users['user'].id, 'lower')
    assert (upper['payer_id'], upper['payee_id'], upper['layer']) == (
        users['root'].id, users['admin'].id, 'upper')
    assert Decimal(lower['payment_amount']) == 80 and Decimal(upper['payment_amount']) == 90
    users['other_user'].parent_id = users['root'].id
    direct, _ = order_case.make(owner='other_user')
    _rate(db, users, order_case, '60', owner='other_user', payer='root')
    db.commit()
    order = _create(login('root'), [direct])
    assert (order['layer'], order['payee_id']) == ('lower', users['other_user'].id)
    assert Decimal(order['payment_amount']) == 60


def test_unauthorized_or_mixed_accounts_fail_as_one_batch_and_history_uses_frozen_parties(
        db, users, order_case, login):
    channel, _ = order_case.pair
    foreign, _ = order_case.make(owner='other_user')
    sibling, _ = order_case.make(owner='sibling')
    db.commit()
    admin = login('admin')
    user = login('user')
    assert user.post(ORDERS+'/preview', json={'channel_ids': [channel.id]}).status_code == 403
    assert admin.post(ORDERS+'/preview', json={'channel_ids': [channel.id, foreign.id]}).status_code == 404
    mixed = admin.post(ORDERS+'/preview', json={'channel_ids': [channel.id, sibling.id]})
    assert mixed.status_code in (409, 422), mixed.text
    assert _counts(db) == (0, 0)
    order = _create(admin, [channel])
    frozen = _detail(admin, order)
    users['user'].parent_id = users['other_admin'].id
    db.commit()
    assert _detail(user, order) == frozen
    assert _detail(admin, order) == frozen
    assert _detail(login('root'), order) == frozen
    assert login('other_admin').get(ORDERS+'/'+order['id']).status_code == 404
    assert login('other_user').get(ORDERS+'/'+order['id']).status_code == 404




def test_new_orders_and_history_reads_preserve_existing_orders_and_usage_facts(
        db, users, order_case, login):
    api = login('admin')
    old_pair = order_case.make(quota=2_500_000)
    db.commit()
    old_order = _create(api, [old_pair[0]])
    order_case.fact(old_pair, '9999')
    db.commit()
    original = _usage_snapshot(db)
    old_detail = _detail(api, old_order)
    order = _create(api, [order_case.pair[0]])
    assert _usage_snapshot(db) == original
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        detail = _detail(api, order)
        listing = api.get(ORDERS, params={'account_id': users['user'].id, 'limit': 1, 'offset': 0})
        repeated = _detail(api, order)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert listing.status_code == 200, listing.text
    assert listing.json()['total'] == 2 and listing.json()['items'][0]['id'] == order['id']
    assert detail == repeated and _usage_snapshot(db) == original
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert _detail(api, old_order) == old_detail
    for payload in (detail, listing.json()):
        serialized = str(payload)
        assert 'fixture-private-key' not in serialized
        assert 'fixture-site-token' not in serialized
        assert 'fixture-order-snapshot-secret' not in serialized
        assert 'key_encrypted' not in serialized and 'remote_snapshot' not in serialized


def test_order_detail_preserves_a_valid_channel_number_above_signed_32_bit(db, order_case, login):
    db.execute(text("SELECT setval(pg_get_serial_sequence('channels', 'display_id'), :value, false)"),
               {'value': 2**31+1})
    channel, _ = order_case.make()
    db.commit()
    api = login('admin')
    quote = _preview(api, [channel])
    assert _line(quote, channel)['display_id'] == 2**31+1
    order = _create(api, [channel], quote)
    assert _line(_detail(api, order), channel)['display_id'] == 2**31+1
    with SessionLocal() as observer:
        stored = observer.scalar(select(SettlementOrderLine).where(SettlementOrderLine.order_id == order['id']))
        assert stored.display_id == 2**31+1


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Local purge uses PostgreSQL advisory locks')
def test_order_link_blocks_last_distribution_purge_validation_without_changing_data(db, users, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')
    order = _create(api, [channel])
    original_order = _detail(api, order)

    def snapshot():
        models = (Channel, Distribution, UploadGroup, SettlementOrder, SettlementOrderLine)
        return {model.__tablename__: {row.id: deepcopy({column.key: getattr(row, column.key)
                for column in model.__table__.columns}) for row in db.scalars(
                    select(model).execution_options(populate_existing=True))}
                for model in models}

    before = snapshot(), _usage_snapshot(db)
    # The service contract requires the caller to hold the owner/channel locks.
    db.execute(select(User.id).where(User.id == channel.owner_id).with_for_update()).all()
    db.execute(select(Channel.id).where(Channel.id == channel.id).with_for_update()).all()
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with pytest.raises(HTTPException) as rejected:
            purge_distributions(db, users['admin'], channel, [dist.id], validate_only=True)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert rejected.value.status_code == 409
    assert '结算单' in str(rejected.value.detail)
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert not db.new and not db.dirty and not db.deleted
    db.rollback()
    assert (snapshot(), _usage_snapshot(db)) == before
    assert _detail(api, order) == original_order


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Overlapping orders require PostgreSQL row locks')
def test_overlapping_creation_waits_for_first_transaction_then_rejects_stale_quote(db, users, order_case):
    channel, _ = order_case.pair
    other, _ = order_case.make(quota=5_000_000)
    actor_id, first_id, other_id = users['admin'].id, channel.id, other.id
    db.commit()
    first_quote = preview_order(db, users['admin'], [first_id])
    overlapping_quote = preview_order(db, users['admin'], [first_id, other_id])
    db.commit()
    with SessionLocal() as holder, SessionLocal() as contender:
        first = create_order(holder, holder.get(User, actor_id), [first_id], first_quote['snapshot_hash'], uid())
        contender.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as blocked:
            create_order(contender, contender.get(User, actor_id), [first_id, other_id],
                         overlapping_quote['snapshot_hash'], uid())
        assert (getattr(blocked.value.orig, 'sqlstate', None)
                or getattr(blocked.value.orig, 'pgcode', None)) == '55P03'
        contender.rollback()
        holder.commit()
        with pytest.raises(HTTPException) as rejected:
            create_order(contender, contender.get(User, actor_id), [first_id, other_id],
                         overlapping_quote['snapshot_hash'], uid())
        assert rejected.value.status_code == 409
        contender.rollback()
        assert _counts(contender) == (1, 1)
        assert contender.get(SettlementOrder, first.id) is not None


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Rate changes share the PostgreSQL payee lock')
def test_rate_update_waits_for_order_confirmation_and_does_not_rewrite_its_rate(db, users, order_case):
    channel, _ = order_case.pair
    actor_id, channel_id, payee_id = users['admin'].id, channel.id, users['user'].id
    quote = preview_order(db, users['admin'], [channel_id])
    body = DiscountIn(payee_id=payee_id, category_id=channel.category_id, service_variant='bedrock', percent='20')
    db.commit()
    with SessionLocal() as holder, SessionLocal() as changer:
        order = create_order(holder, holder.get(User, actor_id), [channel_id], quote['snapshot_hash'], uid())
        changer.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as blocked:
            set_discount(body, changer, changer.get(User, actor_id))
        assert (getattr(blocked.value.orig, 'sqlstate', None)
                or getattr(blocked.value.orig, 'pgcode', None)) == '55P03'
        changer.rollback()
        holder.commit()
        set_discount(body, changer, changer.get(User, actor_id))
        frozen = changer.scalar(select(SettlementOrderLine).where(SettlementOrderLine.order_id == order.id))
        assert frozen.discount_percent == 80 and frozen.payment_amount == 80
