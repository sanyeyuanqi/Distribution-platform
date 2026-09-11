"""Settlement uses verified saved usage regardless of observed enable/disable state."""
from decimal import Decimal

import pytest
from app.db import SessionLocal, engine, uid
from app.models import Site, User
from app.models_billing import SettlementOrder, SettlementOrderLine
from app.models_channels import Distribution, Task, TaskItem
from app.settlement_orders import create_order, order_group_status, preview_order
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_settlement_orders import (
    ORDERS,
    _create,
    _observe,
    _preview,
)
from test_settlement_orders import (
    order_case as _order_fixture,
)
from test_settlement_orders import (
    service_case as _service_fixture,
)

service_case = _service_fixture
order_case = _order_fixture


def test_settlement_uses_saved_background_observation_without_remote_reads_or_sync_tasks(
        db, order_case, login, monkeypatch):
    def unexpected_remote_read(*args, **kwargs):
        pytest.fail('Settlement must use the saved background observation')

    monkeypatch.setattr('app.worker.get_adapter', unexpected_remote_read)
    monkeypatch.setattr('app.adapters.get_adapter', unexpected_remote_read)
    before = tuple(db.scalar(select(func.count()).select_from(model)) for model in (Task, TaskItem))
    channel, dist = order_case.pair
    _remote(dist, 1)
    db.commit()
    order = _create(login('admin'), [channel])
    assert Decimal(order['usage_amount']) == 100
    assert Decimal(order['payment_amount']) == 80
    assert tuple(db.scalar(select(func.count()).select_from(model)) for model in (Task, TaskItem)) == before


def _remote(dist, status=3, *, local_status=None):
    dist.status = local_status or ('enabled' if status == 1 else 'disabled')
    dist.remote_snapshot = {**dist.remote_snapshot, 'id': dist.remote_id, 'status': status}


def _groups(api, account_id):
    response = api.get('/api/settlements/groups', params={'account_id': account_id})
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert all('order' not in row['settlement'] and 'financials' not in row['settlement'] for row in rows)
    return {row['id']: row['settlement'] for row in rows}


def _preview_response(api, channel_ids):
    return api.post(ORDERS+'/preview', json={'channel_ids': channel_ids})


def _create_response(api, channel_ids, quote):
    return api.post(ORDERS, json={'channel_ids': channel_ids,
        'snapshot_hash': quote['snapshot_hash'], 'idempotency_key': uid()})


def _counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model))
                 for model in (SettlementOrder, SettlementOrderLine))


@pytest.mark.parametrize('local_status', ['disabled', 'unavailable', 'enabled'])
def test_any_automatic_disabled_target_allows_group_with_other_enabled_targets(
        db, users, order_case, login, local_status):
    channel, dist = order_case.pair
    _remote(dist, local_status=local_status)
    order_case.target(channel, status=1)
    db.commit()
    api = login('admin')
    row = _groups(api, users['user'].id)[channel.id]
    state = row['group_state']
    assert state['status'] == 'automatic_disabled'
    assert state['automatic_disabled_count'] == 1 and state['total'] == 2
    assert 'settlement_ready' not in state and row['can_settle'] is True
    quote = _preview(api, [channel])
    assert _counts(db) == (0, 0)
    order = _create(api, [channel], quote)
    assert Decimal(order['usage_amount']) == 110
    assert Decimal(order['payment_amount']) == 88
    assert _counts(db) == (1, 1)


@pytest.mark.parametrize('invalid', ['string-status', 'boolean-status', 'no-status',
    'mismatched-id', 'deleted', 'missing', 'tombstone', 'no-remote-id', 'no-distributions',
    'pending', 'running', 'failed', 'needs_review'])
def test_unproven_remote_usage_identity_or_missing_targets_block_order_preview(db, users, order_case, login, invalid):
    channel, dist = order_case.pair
    channel_id = channel.id
    if invalid == 'string-status':
        _remote(dist, '3')
    elif invalid == 'boolean-status':
        _remote(dist, True)
    elif invalid == 'no-status':
        dist.remote_snapshot = {key: value for key, value in dist.remote_snapshot.items() if key != 'status'}
    elif invalid == 'mismatched-id':
        dist.remote_snapshot = {**dist.remote_snapshot, 'id': 'different-generation'}
    elif invalid in ('deleted', 'missing', 'pending', 'running', 'failed', 'needs_review'):
        dist.status = invalid
    elif invalid == 'tombstone':
        dist.remote_snapshot = {**dist.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    elif invalid == 'no-remote-id':
        dist.remote_id = None
    else:
        db.delete(dist)
    db.commit()
    api = login('admin')
    row = _groups(api, users['user'].id)[channel_id]
    assert 'settlement_ready' not in row['group_state']
    assert row['group_state']['automatic_disabled_count'] == 0 and row['can_settle'] is False
    if invalid == 'no-distributions':
        assert row['group_state']['status'] == 'no_channels' and row['group_state']['total'] == 0
    response = _preview_response(api, [channel_id])
    assert response.status_code == 409, response.text
    assert _counts(db) == (0, 0)


@pytest.mark.parametrize('count', [1, 2], ids=['single-manual', 'all-manual'])
def test_all_manually_disabled_targets_allow_order_preview_and_confirmation(
        db, users, order_case, login, count):
    channel, dist = order_case.pair
    _remote(dist, 2)
    for _ in range(count-1):
        order_case.target(channel, status=2)
    db.commit()
    api = login('admin')
    row = _groups(api, users['user'].id)[channel.id]
    state = row['group_state']
    assert state['status'] == 'manual_disabled'
    assert state['automatic_disabled_count'] == 0 and state['total'] == count
    assert 'settlement_ready' not in state and row['can_settle'] is True
    order = _create(api, [channel])
    assert Decimal(order['usage_amount']) == 100 + 10 * (count-1)
    assert _counts(db) == (1, 1)


@pytest.mark.parametrize('other_status', [1, None], ids=['enabled', 'unknown'])
def test_mixed_states_allow_enabled_targets_but_require_verified_usage_for_every_target(
        db, users, order_case, login, other_status):
    channel, dist = order_case.pair
    _remote(dist, 2)
    other = order_case.target(channel)
    _remote(other, other_status)
    ready, _ = order_case.make()
    db.commit()
    api = login('admin')
    states = _groups(api, users['user'].id)
    assert states[channel.id]['group_state']['status'] == 'mixed'
    assert states[channel.id]['can_settle'] is (other_status == 1)
    assert states[ready.id]['can_settle'] is True
    result = _preview_response(api, [ready.id, channel.id])
    assert result.status_code == (200 if other_status == 1 else 409), result.text
    assert _counts(db) == (0, 0)
    if other_status == 1:
        order = _create(api, [ready, channel], result.json())
        assert Decimal(order['usage_amount']) == 210 and Decimal(order['payment_amount']) == 168
        assert _counts(db) == (1, 2)


@pytest.mark.parametrize('creation_state', ['needs_review', 'created_pending_verification'])
def test_unlinked_uncertain_creation_is_observed_without_changing_linked_target_usage(
        db, users, order_case, login, creation_state):
    channel, known = order_case.pair
    _remote(known, 2)
    uncertain = order_case.target(channel)
    uncertain.remote_id = None
    uncertain.status = creation_state
    uncertain.remote_snapshot = {}
    db.commit()
    api = login('admin')
    manual = _groups(api, users['user'].id)[channel.id]
    assert manual['group_state']['total'] == 2 and manual['group_state']['status'] == 'mixed'
    assert 'settlement_ready' not in manual['group_state'] and manual['can_settle'] is True
    manual_quote = _preview(api, [channel])
    assert Decimal(manual_quote['usage_amount']) == 100
    assert _counts(db) == (0, 0)

    _remote(known, 3)
    db.commit()
    automatic = _groups(api, users['user'].id)[channel.id]
    assert automatic['group_state']['total'] == 2
    assert automatic['group_state']['automatic_disabled_count'] == 1
    assert 'settlement_ready' not in automatic['group_state'] and automatic['can_settle'] is True
    quote = _preview(api, [channel])
    assert Decimal(quote['usage_amount']) == 100
    assert _counts(db) == (0, 0)
    _create(api, [channel], quote)
    assert _counts(db) == (1, 1)


@pytest.mark.parametrize('remote_status', [1, 2, 3], ids=['enabled', 'manual', 'automatic'])
def test_remote_state_never_grants_another_accounts_settlement_permission(
        db, users, order_case, login, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    foreign, _ = order_case.make(owner='other_user')
    db.commit()
    readonly = order_group_status(db, users['user'], [channel.id])[0]
    assert 'settlement_ready' not in readonly['group_state']
    assert readonly['can_manage'] is False and readonly['can_settle'] is False
    db.rollback()
    admin, user, outsider = login('admin'), login('user'), login('other_admin')
    assert user.get('/api/settlements/groups', params={'account_id': users['user'].id}).status_code == 403
    assert _preview_response(user, [channel.id]).status_code == 403
    assert _preview_response(outsider, [channel.id]).status_code == 404
    assert outsider.get('/api/settlements/groups', params={'account_id': users['user'].id}).status_code == 404
    assert _preview_response(admin, [channel.id, foreign.id]).status_code == 404
    assert _counts(db) == (0, 0)


@pytest.mark.parametrize('source', ['zero', 'missing', 'unconverted', 'negative'])
@pytest.mark.parametrize('remote_status', [1, 3, 2], ids=['enabled', 'automatic', 'manual'])
def test_remote_state_does_not_turn_missing_or_invalid_remote_usage_into_zero(
        db, users, order_case, login, source, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    if source == 'zero':
        dist.remote_snapshot = {**dist.remote_snapshot, 'used_quota': 0}
    elif source == 'missing':
        dist.remote_snapshot = {key: value for key, value in dist.remote_snapshot.items() if key != 'used_quota'}
    elif source == 'negative':
        dist.remote_snapshot = {**dist.remote_snapshot, 'used_quota': -1}
    else:
        db.get(Site, dist.site_id).capabilities = {}
    db.commit()
    api = login('admin')
    row = _groups(api, users['user'].id)[channel.id]
    assert 'settlement_ready' not in row['group_state']
    assert row['can_settle'] is (source == 'zero')
    result = _preview_response(api, [channel.id])
    if source == 'zero':
        assert result.status_code == 200, result.text
        order = _create(api, [channel], result.json())
        assert Decimal(order['usage_amount']) == Decimal(order['payment_amount']) == 0
        assert _counts(db) == (1, 1)
    else:
        assert result.status_code == 409, result.text
        assert row['usage_amount'] is None and row['payment_amount'] is None
        assert _counts(db) == (0, 0)


@pytest.mark.parametrize('remote_status', [3, 2], ids=['automatic', 'manual'])
def test_reenabled_target_requires_fresh_snapshot_then_can_be_settled(db, users, order_case, login, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    db.commit()
    api = login('admin')
    quote = _preview(api, [channel])
    _remote(dist, 1)
    db.commit()
    result = _create_response(api, [channel.id], quote)
    assert result.status_code == 409, result.text
    assert _counts(db) == (0, 0)
    # The state is part of frozen evidence, not a settlement prerequisite.
    row = _groups(api, users['user'].id)[channel.id]
    assert row['group_state']['status'] == 'enabled' and row['can_settle'] is True
    enabled_quote = _preview(api, [channel])
    assert enabled_quote['snapshot_hash'] != quote['snapshot_hash']
    _create(api, [channel], enabled_quote)
    assert _counts(db) == (1, 1)


@pytest.mark.parametrize('remote_status', [1, 2, 3], ids=['enabled', 'manual', 'automatic'])
def test_settled_usage_is_not_selectable_again_until_new_remote_usage_arrives(
        db, users, order_case, login, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    db.commit()
    api = login('admin')
    first = _create(api, [channel])
    row = _groups(api, users['user'].id)[channel.id]
    assert 'settlement_ready' not in row['group_state']
    assert row['status'] == 'settled' and row['can_settle'] is False
    assert _preview_response(api, [channel.id]).status_code == 409
    _observe(dist, 58_500_000, status=remote_status)
    db.commit()
    updated = _groups(api, users['user'].id)[channel.id]
    assert updated['status'] == 'unsettled' and updated['can_settle'] is True
    second = _create(api, [channel])
    assert Decimal(second['usage_amount']) == 17 and Decimal(second['payment_amount']) == Decimal('13.6')
    assert _counts(db) == (2, 2)
    assert api.get(ORDERS+'/'+first['id']).json()['usage_amount'] == first['usage_amount']


def test_another_automatic_target_keeps_group_ready_but_confirmation_requires_current_snapshot(
        db, users, order_case, login):
    channel, first = order_case.pair
    second = order_case.target(channel, status=1)
    db.commit()
    api = login('admin')
    quote = _preview(api, [channel])
    _remote(first, 1)
    _remote(second, 3)
    db.commit()
    row = _groups(api, users['user'].id)[channel.id]
    assert row['group_state']['automatic_disabled_count'] == 1 and row['can_settle'] is True
    # Which target was disabled is part of the saved evidence, so require a new quote.
    assert _create_response(api, [channel.id], quote).status_code == 409
    assert _counts(db) == (0, 0)
    _create(api, [channel])
    assert _counts(db) == (1, 1)


def test_zero_baseline_cannot_reopen_same_layer_but_is_independent_for_parent_layer(
        db, users, order_case, login):
    channel, dist = order_case.pair
    _observe(dist, 0, status=2)
    db.commit()
    admin = login('admin')
    _create(admin, [channel])
    lower = _groups(admin, users['user'].id)[channel.id]
    assert lower['status'] == 'settled' and 'settlement_ready' not in lower['group_state']
    assert lower['can_settle'] is False and Decimal(lower['usage_amount']) == 0
    root = login('root')
    upper = _groups(root, users['admin'].id)[channel.id]
    assert upper['status'] == 'unsettled' and upper['can_settle'] is True
    _create(root, [channel])
    assert _counts(db) == (2, 2)


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Snapshot row locks require PostgreSQL')
@pytest.mark.parametrize('remote_status', [3, 2], ids=['automatic', 'manual'])
def test_read_only_preview_does_not_lock_remote_state_and_creation_revalidates(
        db, users, order_case, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    actor_id, channel_id, distribution_id = users['admin'].id, channel.id, dist.id
    snapshot = {**dist.remote_snapshot, 'status': 1}
    db.commit()
    with SessionLocal() as reader, SessionLocal() as updater:
        quote = preview_order(reader, reader.get(User, actor_id), [channel_id])
        updater.execute(text("SET LOCAL lock_timeout = '200ms'"))
        assert updater.execute(update(Distribution).where(Distribution.id == distribution_id).values(
            status='enabled', remote_snapshot=snapshot)).rowcount == 1
        updater.commit()
        with pytest.raises(HTTPException) as rejected:
            create_order(reader, reader.get(User, actor_id), [channel_id], quote['snapshot_hash'], uid())
        assert rejected.value.status_code == 409
        reader.rollback()
        assert _counts(reader) == (0, 0)


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Snapshot row locks require PostgreSQL')
@pytest.mark.parametrize('remote_status', [1, 3, 2], ids=['enabled', 'automatic', 'manual'])
def test_creation_locks_remote_state_until_end_then_revalidates_after_rollback(
        db, users, order_case, remote_status):
    channel, dist = order_case.pair
    _remote(dist, remote_status)
    actor_id, channel_id, distribution_id = users['admin'].id, channel.id, dist.id
    snapshot = {**dist.remote_snapshot, 'status': 1, 'used_quota': 60_000_000}
    quote = preview_order(db, users['admin'], [channel_id])
    db.commit()
    reenable = update(Distribution).where(Distribution.id == distribution_id).values(
        status='enabled', remote_snapshot=snapshot)
    with SessionLocal() as holder, SessionLocal() as updater:
        order = create_order(holder, holder.get(User, actor_id), [channel_id], quote['snapshot_hash'], uid())
        assert holder.get(SettlementOrder, order.id).status == 'settled'
        updater.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as blocked:
            updater.execute(reenable)
        assert (getattr(blocked.value.orig, 'sqlstate', None)
                or getattr(blocked.value.orig, 'pgcode', None)) == '55P03'
        updater.rollback()
        holder.rollback()
        updater.execute(text("SET LOCAL lock_timeout = '200ms'"))
        assert updater.execute(reenable).rowcount == 1
        updater.commit()
    with SessionLocal() as verifier:
        assert verifier.get(Distribution, distribution_id).status == 'enabled'
        assert _counts(verifier) == (0, 0)
        with pytest.raises(HTTPException) as rejected:
            create_order(verifier, verifier.get(User, actor_id), [channel_id], quote['snapshot_hash'], uid())
        assert rejected.value.status_code == 409
        verifier.rollback()
        assert _counts(verifier) == (0, 0)
