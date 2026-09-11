"""Retiring empty legacy tables must never remove current settlement history."""
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
from app.channel_purge import purge_distributions
from app.db import Base, engine, uid, utcnow
from app.main import app
from app.models import User
from app.models_billing import SettlementOrder, SettlementOrderLine, UsageFact
from app.models_channels import Channel, Distribution, UploadGroup
from fastapi import HTTPException
from test_channel_categories import service_case as _service_fixture

service_case = _service_fixture
REVISION = '4f8b27c93a10'
RETIRED = {
    'bills', 'bill_lines', 'payments', 'payment_rules', 'attachments', 'adjustments',
    'settlement_claims', 'model_targets', 'usage_samples',
}


def _migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / f'{REVISION}_retire_unused_empty_tables.py'
    spec = importlib.util.spec_from_file_location('retired_schema_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    return module


def _snapshot(db):
    return {model.__tablename__: deepcopy([dict(row) for row in db.execute(
        sa.select(model.__table__).order_by(model.id)).mappings()])
        for model in (Channel, Distribution, UsageFact, SettlementOrder, SettlementOrderLine)}


@pytest.fixture
def frozen_order(db, users, service_case):
    channel, dist = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    dist.remote_id, dist.status = uid(), 'disabled'
    fact = service_case.fact((channel, dist), '5')
    order = SettlementOrder(id=uid(), number='SOFIXTURE' + uid().replace('-', ''),
        actor_id=users['admin'].id, actor_name='admin', payer_id=users['admin'].id, payer_name='admin',
        payee_id=users['user'].id, payee_name='user', identity_snapshot={'fixture': 'immutable'},
        layer='lower', usage_amount=Decimal('100.123456789'), payment_amount=Decimal('80.0987654312'),
        line_count=1, idempotency_key=uid(), request_hash='r' * 64, snapshot_hash='s' * 64)
    db.add(order)
    db.flush()
    line = SettlementOrderLine(order_id=order.id, channel_id=channel.id, display_id=channel.display_id,
        group_id=channel.group_id, group_tag=db.get(UploadGroup, channel.group_id).tag,
        owner_id=channel.owner_id, owner_name='user', owner_username='user', category_id=channel.category_id,
        category_name='AWS', variant='bedrock', service_name='AWS Bedrock', service_name_en='AWS Bedrock',
        discount_percent=Decimal(80), usage_amount=order.usage_amount, payment_amount=order.payment_amount,
        site_amounts=[{'distribution_id': dist.id, 'site_id': dist.site_id,
                       'cumulative_total': '100.123456789', 'previous_total': '0'}])
    db.add(line)
    db.commit()
    return channel, dist, fact, order


def _legacy_schema(db):
    inspector = sa.inspect(db.connection())
    return {name: {
        'columns': [(column['name'], str(column['type']), column['nullable'], column['default'])
                    for column in inspector.get_columns(name)],
        'primary_key': inspector.get_pk_constraint(name)['constrained_columns'],
        'foreign_keys': sorted((tuple(item['constrained_columns']), item['referred_table'],
                                tuple(item['referred_columns'])) for item in inspector.get_foreign_keys(name)),
        'unique': sorted(tuple(item['column_names']) for item in inspector.get_unique_constraints(name)),
        'indexes': sorted((item['name'], tuple(item['column_names']), item['unique'])
                          for item in inspector.get_indexes(name)),
    } for name in sorted(RETIRED)}


def _capture(db, callback):
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    sa.event.listen(engine, 'before_cursor_execute', capture)
    try:
        callback()
    finally:
        sa.event.remove(engine, 'before_cursor_execute', capture)
    return statements


def test_retired_schema_head_removes_only_the_selected_models():
    root = Path(__file__).parents[1]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == REVISION
    assert scripts.get_revision(REVISION).down_revision == '7a3d91f0c6b2'
    assert RETIRED.isdisjoint(Base.metadata.tables)
    assert {'usage_facts', 'discount_versions', 'settlement_orders', 'settlement_order_lines'} <= Base.metadata.tables.keys()


def test_only_current_settlement_routes_remain_registered():
    paths = set(app.openapi()['paths'])
    assert {path for path in paths if path.startswith('/api/settlements')} == {'/api/settlements/groups'}
    assert '/api/payment-rules' not in paths
    assert {'/api/discounts', '/api/settlement-orders', '/api/settlement-orders/preview'} <= paths


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='DDL and locks use PostgreSQL')
def test_empty_upgrade_and_downgrade_preserve_current_orders_and_rebuild_original_constraints(db, frozen_order):
    before = _snapshot(db)
    migration = _migration(db)
    try:
        migration.downgrade()
        schema = _legacy_schema(db)
        assert schema['bill_lines']['foreign_keys'] == [
            (('bill_id',), 'bills', ('id',)), (('discount_id',), 'discount_versions', ('id',)),
            (('fact_id',), 'usage_facts', ('id',)),
        ]
        assert ('layer', 'payer_id', 'payee_id', 'fact_id') in schema['settlement_claims']['unique']
        assert ('distribution_id', 'source_id') in schema['usage_samples']['unique']
        statements = _capture(db, migration.upgrade)
        assert statements[0].startswith('lock table ')
        assert 'in access exclusive mode' in statements[0]
        assert all('"' + name + '"' in statements[0] for name in RETIRED)
        assert sum(statement.startswith('select exists') for statement in statements) == len(RETIRED)
        first_drop = next(index for index, statement in enumerate(statements) if statement.startswith('drop table'))
        assert first_drop > len(RETIRED)
        assert not any('cascade' in statement for statement in statements)
        assert RETIRED.isdisjoint(sa.inspect(db.connection()).get_table_names())
        assert _snapshot(db) == before
        migration.downgrade()
        assert _legacy_schema(db) == schema
        for name in RETIRED:
            assert db.scalar(sa.text('SELECT COUNT(*) FROM "' + name + '"')) == 0
        assert _snapshot(db) == before
        migration.upgrade()
    finally:
        db.rollback()


def _legacy_row(db, table_name, identities):
    """Populate required columns and FK parents without reviving retired ORM classes."""
    if table_name in identities:
        return identities[table_name]
    table = sa.Table(table_name, sa.MetaData(), autoload_with=db.connection())
    values = {}
    for column in table.columns:
        if column.nullable:
            continue
        if column.foreign_keys:
            reference = next(iter(column.foreign_keys)).column.table.name
            values[column.name] = _legacy_row(db, reference, identities)
        elif column.name == 'id':
            values[column.name] = uid()
        elif isinstance(column.type, sa.JSON):
            values[column.name] = {}
        elif isinstance(column.type, sa.DateTime):
            values[column.name] = utcnow()
        elif isinstance(column.type, sa.Numeric):
            values[column.name] = Decimal(1)
        elif isinstance(column.type, sa.Integer):
            values[column.name] = 6
        else:
            values[column.name] = 'fixture'
    db.execute(sa.insert(table).values(**values))
    identities[table_name] = values['id']
    return values['id']


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='DDL and locks use PostgreSQL')
@pytest.mark.parametrize('occupied_table', sorted(RETIRED))
def test_upgrade_refuses_each_nonempty_retired_table_before_dropping_anything(db, users, frozen_order, occupied_table):
    channel, dist, fact, _ = frozen_order
    before = _snapshot(db)
    migration = _migration(db)
    try:
        migration.downgrade()
        legacy_id = _legacy_row(db, occupied_table, {
            'users': users['user'].id, 'categories': channel.category_id, 'sites': dist.site_id,
            'distributions': dist.id, 'usage_facts': fact.id,
        })
        statements = []

        def attempt():
            with pytest.raises(RuntimeError, match='Refusing to remove non-empty historical tables:') as rejected:
                migration.upgrade()
            assert occupied_table in str(rejected.value)

        statements = _capture(db, attempt)
        assert statements[0].startswith('lock table ')
        assert all('"' + name + '"' in statements[0] for name in RETIRED)
        assert not any(statement.startswith(('drop ', 'delete ', 'truncate ')) for statement in statements)
        assert RETIRED <= set(sa.inspect(db.connection()).get_table_names())
        assert db.scalar(sa.text('SELECT id FROM "' + occupied_table + '" WHERE id = :id'), {'id': legacy_id}) == legacy_id
        assert _snapshot(db) == before
    finally:
        db.rollback()


@pytest.mark.parametrize(('method', 'path', 'body'), [
    ('get', '/api/model-targets', None), ('put', '/api/model-targets', {'items': []}),
    ('post', '/api/usage/samples', {}),
])
def test_retired_statistics_endpoints_are_not_registered(login, method, path, body):
    api = login('root')
    response = getattr(api, method)(path, **({'json': body} if body is not None else {}))
    assert response.status_code == 404


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Channel purge uses PostgreSQL locks')
def test_order_reference_still_blocks_last_distribution_deletion_without_writes(db, users, frozen_order):
    channel, dist, _, _ = frozen_order
    before = _snapshot(db)
    db.execute(sa.select(User.id).where(User.id == channel.owner_id).with_for_update()).all()
    db.execute(sa.select(Channel.id).where(Channel.id == channel.id).with_for_update()).all()

    def attempt():
        with pytest.raises(HTTPException) as rejected:
            purge_distributions(db, users['admin'], channel, [dist.id], validate_only=True)
        assert rejected.value.status_code == 409 and '结算单' in str(rejected.value.detail)

    statements = _capture(db, attempt)
    assert not any(statement.startswith(('insert ', 'update ', 'delete ')) for statement in statements)
    db.rollback()
    assert _snapshot(db) == before


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Channel purge uses PostgreSQL locks')
def test_explicit_unsettled_channel_deletion_keeps_existing_fact_scope_and_needs_no_retired_tables(
        db, users, service_case):
    selected = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    untouched = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner='other_user')
    first_fact = service_case.fact(selected, '10')
    other_fact = service_case.fact(untouched, '20')
    db.commit()
    channel, dist = selected
    selected_fact_id, other_fact_id = first_fact.id, other_fact.id
    db.execute(sa.select(User.id).where(User.id == channel.owner_id).with_for_update()).all()
    db.execute(sa.select(Channel.id).where(Channel.id == channel.id).with_for_update()).all()
    statements = _capture(db, lambda: purge_distributions(db, users['admin'], channel, [dist.id]))
    assert any('from usage_facts' in statement and 'for update' in statement for statement in statements)
    assert not any('from ' + name in statement or 'delete from ' + name in statement for name in RETIRED for statement in statements)
    db.commit()
    assert db.scalar(sa.select(UsageFact.id).where(UsageFact.id == selected_fact_id)) is None
    assert db.scalar(sa.select(UsageFact.id).where(UsageFact.id == other_fact_id)) == other_fact_id
    assert db.get(Channel, untouched[0].id) is not None
