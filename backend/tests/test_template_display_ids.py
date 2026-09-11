"""Receiving-template numbers remain stable independently of site IDs and filters."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.db import Base, SessionLocal, engine
from app.models import AuditEvent, Category, CredentialFormat, SiteUploadTemplate
from app.models_channels import Distribution, Task, TaskItem
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_site_display_ids import add_history, history_snapshot, site


def migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'a730bc28d594_upload_template_display_ids.py'
    spec = importlib.util.spec_from_file_location('template_display_ids_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'f629a17e4b82'
    return module


@pytest.fixture
def template_catalog(db):
    categories, formats = [], []
    for family, code, remote_type in [('OpenAI', 'api_key-v1', 1), ('Anthropic', 'newapi-14-api-key-v1', 14)]:
        category = Category(name=family, family=family)
        db.add(category)
        db.flush()
        fmt = CredentialFormat(category_id=category.id, code=code, name=family, version='1', enabled=True,
            schema_config={'type': 'api_key', 'remote_type': remote_type})
        db.add(fmt)
        categories.append(category)
        formats.append(fmt)
    sites = [site(f'template-site-{index}', enabled=True) for index in range(3)]
    db.add_all(sites)
    db.commit()
    return categories, formats, sites


def template(catalog, *, site_index=0, category_index=0, **values):
    categories, formats, sites = catalog
    return SiteUploadTemplate(site_id=sites[site_index].id, category_id=categories[category_index].id,
        format_id=formats[category_index].id, **values)


def other_history(db):
    result = history_snapshot(db)
    result.pop('site_upload_templates')
    result['sites'] = [dict(row) for row in db.execute(text('SELECT * FROM sites ORDER BY id')).mappings()]
    return result


@pytest.mark.parametrize('populated', [False, True])
def test_template_migration_stable_backfill_preserves_uuid_settings_and_frozen_history(db, populated):
    if populated:
        rows = [site(f'historical-{index}', archived=index == 3) for index in range(4)]
        db.add_all(rows)
        db.flush()
        add_history(db, rows)
        templates = list(db.scalars(select(SiteUploadTemplate).order_by(SiteUploadTemplate.display_id)))
        for row, (suffix, month) in zip(templates, [(9, 1), (7, 3), (3, 2), (1, 2)], strict=True):
            row.id = f'00000000-0000-0000-0000-{suffix:012d}'
            row.created_at = datetime(2025, month, 1, tzinfo=UTC).replace(tzinfo=None)
            row.enabled = suffix != 1
            row.models, row.routing_group, row.remark = ['original-model'], '普通,高级', 'Original note'
            row.channel_config, row.version = {'status': 2}, suffix
            db.flush()
            distribution = db.scalar(select(Distribution).where(Distribution.site_id == row.site_id))
            distribution.upload_template_id = row.id
            distribution.template_version = row.version
            distribution.template_snapshot = {'id': row.id, 'models': row.models, 'routing_group': row.routing_group}
            item = db.scalar(select(TaskItem).where(TaskItem.site_id == row.site_id))
            item.snapshot = {'upload_template_id': row.id, 'template_version': row.version,
                             'template_config': distribution.template_snapshot}
            task = db.get(Task, item.task_id)
            task.snapshot = {**task.snapshot, 'templates': [value.id for value in templates]}
            db.add(AuditEvent(actor_id=task.actor_id, action='upload_template.update', object_type='upload_template',
                object_id=row.id, site_id=row.site_id, summary={'template_id': row.id, 'version': row.version}))
        db.flush()
    module = migration(db)
    module.downgrade()
    before = [dict(row) for row in db.execute(text('SELECT * FROM site_upload_templates ORDER BY created_at,id')).mappings()]
    before_history = other_history(db)
    module.upgrade()
    after = [dict(row) for row in db.execute(text('SELECT * FROM site_upload_templates ORDER BY created_at,id')).mappings()]
    assert [row['display_id'] for row in after] == list(range(1, len(before) + 1))
    assert [{key: value for key, value in row.items() if key != 'display_id'} for row in after] == before
    assert other_history(db) == before_history
    if populated:
        value = db.scalar(select(SiteUploadTemplate).limit(1))
        fresh = SiteUploadTemplate(site_id=value.site_id, category_id=value.category_id,
            format_id=value.format_id, variant='fixture-new-variant')
    else:
        category = Category(name='Empty fixture', family='OpenAI')
        fresh_site = site('empty-fixture')
        db.add_all([category, fresh_site])
        db.flush()
        fmt = CredentialFormat(category_id=category.id, code='fixture-format', name='Fixture')
        db.add(fmt)
        db.flush()
        fresh = SiteUploadTemplate(site_id=fresh_site.id, category_id=category.id, format_id=fmt.id)
    db.add(fresh)
    db.commit()
    assert fresh.display_id == len(before) + 1
    assigned = dict(db.execute(select(SiteUploadTemplate.id, SiteUploadTemplate.display_id)).all())
    db.close()
    with SessionLocal() as reopened:
        assert dict(reopened.execute(select(SiteUploadTemplate.id, SiteUploadTemplate.display_id)).all()) == assigned


def test_clean_upgrade_and_downgrade_match_template_metadata(db):
    assert engine.dialect.name == 'postgresql' and 'test' in engine.url.database.lower()
    db.close()
    Base.metadata.drop_all(engine)
    config = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).parents[1] / 'migrations'))
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        command.upgrade(config, 'head')
        site_row = site('fresh-install')
        db.add(site_row)
        db.flush()
        add_history(db, [site_row])
        db.commit()
        original = db.scalar(select(SiteUploadTemplate))
        original_id = original.id
        assert original.display_id == 1
        db.close()
        command.upgrade(config, 'head')
        command.downgrade(config, 'f629a17e4b82')
        command.upgrade(config, 'head')
        context = MigrationContext.configure(db.connection(), opts={
            'include_object': lambda obj, name, type_, reflected, compare_to:
                type_ != 'table' or name == 'site_upload_templates'})
        assert compare_metadata(context, Base.metadata) == []
        assert db.get(SiteUploadTemplate, original_id).display_id == 1
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == head
    finally:
        db.rollback()
        with engine.begin() as connection:
            connection.execute(text('DROP TABLE IF EXISTS alembic_version'))


def test_template_identity_database_constraints_and_client_assignment_rejection(db, template_catalog):
    inspector = inspect(db.connection())
    column = next(column for column in inspector.get_columns('site_upload_templates') if column['name'] == 'display_id')
    assert column['nullable'] is False
    assert column['identity']['always'] is True and column['identity']['minvalue'] == 1
    assert column['identity']['start'] == 1 and column['identity']['cycle'] is False
    assert any(value['column_names'] == ['display_id'] for value in inspector.get_unique_constraints('site_upload_templates'))
    assert any(value['name'] == 'ck_upload_template_display_id_positive'
               for value in inspector.get_check_constraints('site_upload_templates'))
    for value in (0, -1, 999):
        with pytest.raises(DBAPIError), db.begin_nested():
            db.add(template(template_catalog, display_id=value))
            db.flush()


def test_concurrent_template_numbers_are_unique_and_not_reused_after_delete_or_rollback(db, template_catalog):
    barrier = Barrier(6)
    def create(index):
        with SessionLocal() as session:
            value = template(template_catalog, variant=f'parallel-{index}')
            session.add(value)
            barrier.wait(timeout=10)
            session.commit()
            return value.id, value.display_id
    with ThreadPoolExecutor(max_workers=6) as pool:
        created = list(pool.map(create, range(6)))
    assert len({number for _, number in created}) == 6
    last_id, last_number = max(created, key=lambda pair: pair[1])
    db.delete(db.get(SiteUploadTemplate, last_id))
    db.commit()
    discarded = template(template_catalog, variant='discarded')
    db.add(discarded)
    db.flush()
    discarded_number = discarded.display_id
    db.rollback()
    replacement = template(template_catalog, variant='replacement')
    db.add(replacement)
    db.commit()
    assert replacement.display_id > discarded_number > last_number > 0
    assert dict(db.execute(select(SiteUploadTemplate.id, SiteUploadTemplate.display_id)
                          .where(SiteUploadTemplate.id != replacement.id)).all()) == {
                              id_: number for id_, number in created if id_ != last_id}


def create_body(catalog, site_index=0, category_index=0, **values):
    categories, formats, sites = catalog
    return {'site_id': sites[site_index].id, 'category_id': categories[category_index].id,
        'format_id': formats[category_index].id, 'enabled': False, **values}


def test_api_template_numbers_ascending_filters_and_uuid_crud_stay_stable(db, login, template_catalog):
    root = login('root')
    created = []
    for site_index, category_index in [(1, 0), (0, 1), (0, 0)]:
        response = root.post('/api/upload-templates', json=create_body(template_catalog, site_index, category_index))
        assert response.status_code == 201, response.text
        value = response.json()
        assert type(value['display_id']) is int and len(value['id']) == 36
        created.append(value)
    assert [value['display_id'] for value in created] == [1, 2, 3]
    for index, value in enumerate(created):
        db.get(SiteUploadTemplate, value['id']).created_at = datetime(2030 - index, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    db.commit()
    def listed(**params):
        return root.get('/api/upload-templates', params=params).json()['items']
    assert [row['display_id'] for row in listed()] == [1, 2, 3]
    assert [row['display_id'] for row in listed(site_id=template_catalog[2][0].id)] == [2, 3]
    assert [row['display_id'] for row in listed(category_id=template_catalog[0][0].id)] == [1, 3]
    assert [row['display_id'] for row in listed(site_id=template_catalog[2][0].id,
                                               category_id=template_catalog[0][0].id)] == [3]
    middle = created[1]
    assert root.patch('/api/upload-templates/' + str(middle['display_id']), json={'name': 'Wrong ID'}).status_code == 404
    updated = root.patch('/api/upload-templates/' + middle['id'], json={'name': 'Renamed'})
    assert updated.status_code == 200 and updated.json()['display_id'] == 2
    assert root.delete('/api/upload-templates/' + middle['id']).status_code == 200
    assert [row['display_id'] for row in listed()] == [1, 3]
    replacement = root.post('/api/upload-templates', json=create_body(template_catalog, 0, 1))
    assert replacement.status_code == 201 and replacement.json()['display_id'] == 4


@pytest.mark.parametrize('value', [1, 999, '9', None])
def test_template_number_not_client_assignable_or_editable(db, login, template_catalog, value):
    stored = template(template_catalog)
    db.add(stored)
    db.commit()
    root = login('root')
    assert root.post('/api/upload-templates', json=create_body(template_catalog, 1, display_id=value)).status_code == 422
    assert root.patch('/api/upload-templates/' + stored.id, json={'display_id': value}).status_code == 422
    db.expire_all()
    assert db.get(SiteUploadTemplate, stored.id).display_id == 1


@pytest.mark.parametrize('actor', ['admin', 'user'])
def test_number_does_not_grant_template_management_permissions(db, login, template_catalog, actor):
    stored = template(template_catalog)
    db.add(stored)
    db.commit()
    client = login(actor)
    assert client.get('/api/upload-templates', params={'site_id': stored.site_id}).status_code == 403
    assert client.post('/api/upload-templates', json=create_body(template_catalog, 1)).status_code == 403
    for identifier in (stored.id, str(stored.display_id)):
        assert client.patch('/api/upload-templates/' + identifier, json={'name': 'Forbidden'}).status_code == 403
        assert client.delete('/api/upload-templates/' + identifier).status_code == 403
