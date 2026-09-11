"""Removing redundant aggregates must preserve frozen orders and target baselines."""
import importlib.util
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.db import engine, uid
from app.models_billing import SettlementOrder, SettlementOrderLine
from test_settlement_orders import (
    ORDERS,
    _counts,
    _create,
    _detail,
    _line,
    _observe,
    _preview,
)
from test_settlement_orders import order_case as _order_fixture
from test_settlement_orders import service_case as _service_fixture

order_case = _order_fixture
service_case = _service_fixture


REVISION = '7a3d91f0c6b2'
REMOVED = {'cumulative_total', 'previous_total'}


def _migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / f'{REVISION}_simplify_settlement_line_amounts.py'
    spec = importlib.util.spec_from_file_location('settlement_line_amount_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    return module


def _snapshot(db, model):
    return deepcopy([dict(row) for row in db.execute(sa.select(model.__table__)).mappings()])


def test_settlement_amount_migration_is_in_current_chain_and_matches_model():
    root = Path(__file__).parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    scripts = ScriptDirectory.from_config(config)
    assert REVISION in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision(REVISION).down_revision == '19ac74d3e860'
    assert not REMOVED.intersection(SettlementOrderLine.__table__.columns.keys())


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Settlement migrations use PostgreSQL')
def test_upgrade_preserves_historical_order_and_settles_only_new_target_consumption(db, order_case, login):
    channel, dist = order_case.pair
    api = login('admin')
    quote = _preview(api, [channel])
    key = uid()
    order = _create(api, [channel], quote, key=key)
    original_detail = _detail(api, order)
    orders = _snapshot(db, SettlementOrder)
    lines = _snapshot(db, SettlementOrderLine)

    # Recreate the previous schema independently of downgrade, with real frozen history.
    ops = Operations(MigrationContext.configure(db.connection()))
    for name in REMOVED:
        ops.add_column('settlement_order_lines', sa.Column(name, sa.Numeric(), nullable=True))
    db.execute(sa.text('UPDATE settlement_order_lines SET cumulative_total = 100, previous_total = 0'))
    for name in REMOVED:
        ops.alter_column('settlement_order_lines', name, nullable=False)
    assert REMOVED.issubset({column['name'] for column in sa.inspect(db.connection()).get_columns('settlement_order_lines')})
    _migration(db).upgrade()
    columns = {column['name'] for column in sa.inspect(db.connection()).get_columns('settlement_order_lines')}
    assert columns == set(SettlementOrderLine.__table__.columns.keys())
    # These full snapshots include amounts, rates, site_amounts and idempotency hashes.
    assert _snapshot(db, SettlementOrder) == orders
    assert _snapshot(db, SettlementOrderLine) == lines
    db.commit()

    assert _detail(api, order) == original_detail
    assert _create(api, [channel], quote, key=key) == order
    assert _counts(db) == (1, 1)
    _observe(dist, 62_500_000)
    db.commit()
    incremental = _preview(api, [channel])
    line = _line(incremental, channel)
    assert Decimal(line['usage_amount']) == 25 and Decimal(line['payment_amount']) == 20
    assert Decimal(line['site_amounts'][0]['previous_total']) == 100
    assert Decimal(line['site_amounts'][0]['cumulative_total']) == 125
    second = _create(api, [channel], incremental)
    second_line = _line(_detail(api, second), channel)
    assert Decimal(second_line['usage_amount']) == 25 and Decimal(second_line['payment_amount']) == 20
    assert _detail(api, order) == original_detail
    for path, payload in (
        (ORDERS+'/preview', {'channel_ids': [channel.id]}),
        (ORDERS, {'channel_ids': [channel.id], 'snapshot_hash': incremental['snapshot_hash'],
                  'idempotency_key': uid()}),
    ):
        response = api.post(path, json=payload)
        assert response.status_code == 409, response.text
    assert _counts(db) == (2, 2)


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Downgrade sums PostgreSQL JSON using NUMERIC')
@pytest.mark.parametrize(('sources', 'cumulative', 'previous'), [
    ([
        {'active': True, 'cumulative_total': '9007199254740993.123456789012345678',
         'previous_total': '9007199254740992.000000000000000001'},
        {'cumulative_total': '0.000000000000000002', 'previous_total': '0.000000000000000003'},
        {'active': False, 'cumulative_total': '99999999999999999.99', 'previous_total': '88888888888888888.88'},
    ], '9007199254740993.123456789012345680', '9007199254740992.000000000000000004'),
    ([{'active': False, 'cumulative_total': '100', 'previous_total': '75'}], '0', '0'),
], ids=['exact-active-and-implicit-active-sums', 'only-inactive-targets'])
def test_downgrade_rebuilds_aggregates_exactly_without_rewriting_frozen_values(
        db, order_case, login, sources, cumulative, previous):
    channel, _ = order_case.pair
    order = _create(login('admin'), [channel])
    line = db.scalar(sa.select(SettlementOrderLine).where(SettlementOrderLine.order_id == order['id']))
    line.site_amounts = sources
    db.flush()
    orders = _snapshot(db, SettlementOrder)
    lines = _snapshot(db, SettlementOrderLine)
    _migration(db).downgrade()
    restored = db.execute(sa.text('SELECT cumulative_total, previous_total FROM settlement_order_lines')).one()
    assert restored == (Decimal(cumulative), Decimal(previous))
    columns = {column['name']: column for column in sa.inspect(db.connection()).get_columns('settlement_order_lines')}
    assert all(columns[name]['nullable'] is False for name in REMOVED)
    assert _snapshot(db, SettlementOrder) == orders
    assert _snapshot(db, SettlementOrderLine) == lines
    _migration(db).upgrade()
    assert _snapshot(db, SettlementOrderLine) == lines
