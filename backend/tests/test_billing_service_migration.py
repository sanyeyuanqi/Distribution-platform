"""Upgrade the real old schema without altering monetary or discount history."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models_billing import DiscountVersion, UsageFact
from sqlalchemy import select, text
from test_billing_service_discounts import add_rate, import_services
from test_billing_service_discounts import service_ledger as service_ledger_fixture

service_ledger = service_ledger_fixture


def test_upgrade_preserves_old_defaults_and_freezes_only_unambiguous_usage(db, users, service_ledger):
    uncertain = service_ledger['services'][('Google', 'vertex_gemini')]
    uncertain['format'].schema_config = {'type': 'vertex_json', 'remote_type': 41}
    uncertain['channel'].models = ['gemini-2.5-pro']
    for family in service_ledger['categories']:
        add_rate(db, users, service_ledger, family, '72.125')
    facts = import_services(db, users, service_ledger)
    original = {fact.id: (fact.service_variant, fact.amount, fact.raw_amount, fact.conversion_version) for fact in facts}
    discounts = {row.id: (row.percent, row.effective_at) for row in db.scalars(select(DiscountVersion))}
    assert len(discounts) == 7
    db.commit()
    for table, column in [('usage_facts', 'service_variant'), ('discount_versions', 'service_variant'),
                          ('discount_versions', 'inherits_category')]:
        db.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))

    path = Path(__file__).resolve().parents[1] / 'migrations/versions/e849c25a6d10_service_discounts.py'
    spec = importlib.util.spec_from_file_location('service_discounts_migration_fixture', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Operations(MigrationContext.configure(db.connection()))
    migration.upgrade()
    db.expire_all()
    assert {fact.id: (fact.service_variant, fact.amount, fact.raw_amount, fact.conversion_version)
            for fact in db.scalars(select(UsageFact))} == original
    assert {row.id: (row.percent, row.effective_at) for row in db.scalars(select(DiscountVersion))} == discounts
    assert all(row.service_variant is None and row.inherits_category is False
               for row in db.scalars(select(DiscountVersion)))

    add_rate(db, users, service_ledger, 'AWS', '25', 'bedrock')
    db.flush()
    with pytest.raises(RuntimeError, match='refusing a lossy downgrade'):
        migration.downgrade()
    assert len(list(db.scalars(select(UsageFact)))) == 11
