"""Archival releases identities without rewriting historical site records."""
import importlib.util
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db import Base
from app.models import Site
from app.models_channels import Task, TaskItem
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError


def migration(db):
    path = Path(__file__).parents[1] / 'migrations/versions/6c28f3a71e90_active_site_identity.py'
    spec = importlib.util.spec_from_file_location('active_site_identity_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    return module


def site(label, **values):
    return Site(name=label, prefix=label, base_url=f'https://{label}.invalid',
                seller_user_id='71', token_encrypted='historical-fixture-ciphertext', **values)


def snapshot(db, model):
    return [dict(row) for row in db.execute(select(model.__table__).order_by(model.id)).mappings()]


def assert_active_indexes(db):
    indexes = {index['name']: index for index in inspect(db.connection()).get_indexes('sites')}
    for column in ('prefix', 'base_url'):
        index = indexes[f'uq_sites_active_{column}']
        assert index['unique'] and index['column_names'] == [column]
        assert 'archived' in index['dialect_options']['postgresql_where']
    assert not any(row['column_names'] in (['prefix'], ['base_url'])
                   for row in inspect(db.connection()).get_unique_constraints('sites'))


def test_upgrade_preserves_sites_and_linked_tasks_and_matches_metadata(db, users):
    archived = site('archived', archived=True, enabled=False, capabilities={'historical': True})
    active = site('active', enabled=True)
    db.add_all([archived, active])
    db.flush()
    task = Task(actor_id=users['root'].id, owner_id=users['root'].id,
                actor_session_version=1, kind='sync')
    db.add(task)
    db.flush()
    db.add(TaskItem(task_id=task.id, site_id=archived.id, operation='sync', status='succeeded',
                    snapshot={'site_base_url': archived.base_url, 'seller_user_id': '71'}))
    db.flush()
    module = migration(db)
    module.downgrade()
    before = {model: snapshot(db, model) for model in (Site, Task, TaskItem)}
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(site('archived'))
        db.flush()

    module.upgrade()
    assert_active_indexes(db)
    assert {model: snapshot(db, model) for model in before} == before
    context = MigrationContext.configure(db.connection(), opts={
        'include_object': lambda obj, name, type_, reflected, compare_to: type_ != 'table' or name == 'sites'})
    assert compare_metadata(context, Base.metadata) == []

    replacement = site('archived')
    db.add(replacement)
    db.flush()
    assert replacement.id != archived.id and replacement.display_id > active.display_id
    assert snapshot(db, TaskItem) == before[TaskItem]
    for row in before[Site]:
        assert next(current for current in snapshot(db, Site) if current['id'] == row['id']) == row
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(site('archived'))
        db.flush()


@pytest.mark.parametrize('column', ['prefix', 'base_url'])
def test_downgrade_rejects_reused_identity_without_modifying_history(db, column):
    archived = site('original', archived=True)
    active = site('replacement')
    setattr(active, column, getattr(archived, column))
    db.add_all([archived, active])
    db.flush()
    before = snapshot(db, Site)
    with pytest.raises(RuntimeError, match='archived identity has been reused'), db.begin_nested():
        migration(db).downgrade()
    assert snapshot(db, Site) == before
    assert_active_indexes(db)


def test_downgrade_and_upgrade_without_reused_identities_preserve_records(db):
    db.add_all([site('first', archived=True), site('second')])
    db.flush()
    before = snapshot(db, Site)
    module = migration(db)
    module.downgrade()
    constraints = inspect(db.connection()).get_unique_constraints('sites')
    assert {'sites_prefix_key', 'sites_base_url_key'} <= {row['name'] for row in constraints}
    module.upgrade()
    assert snapshot(db, Site) == before
    assert_active_indexes(db)
