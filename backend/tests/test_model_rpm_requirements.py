"""Model demand is strict template metadata, never an upstream rate limit."""
# ruff: noqa: F811
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app import worker
from app.models import AuditEvent, SiteUploadTemplate
from app.models_channels import Distribution, TaskItem
from app.upload_templates import template_config
from sqlalchemy import func, inspect, select, text
from test_site_display_ids import add_history, history_snapshot, site
from test_upload_templates import (  # noqa: F401
    catalog,
    no_network,
    submit,
    template_body,
    upload,
)


def create(root, catalog, **changes):
    response = root.post('/api/upload-templates', json=template_body(catalog, models=['model-a', 'model-b'], **changes))
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_detail_and_patch_roundtrip_demand_and_audit(db, login, catalog):
    root = login('root')
    initial = create(root, catalog, model_rpm_requirements={'model-a': 1, 'model-b': 1_000_000})
    path = '/api/upload-templates/' + initial['id']
    assert initial['model_rpm_requirements'] == {'model-a': 1, 'model-b': 1_000_000}
    assert root.get(path).json()['model_rpm_requirements'] == initial['model_rpm_requirements']
    assert root.get('/api/upload-templates').json()['items'][0]['model_rpm_requirements'] == initial['model_rpm_requirements']
    response = root.patch(path, json={'model_rpm_requirements': {'model-b': 700}})
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed['model_rpm_requirements'] == {'model-b': 700} and changed['version'] == initial['version'] + 1
    assert changed['models'] == initial['models'] and changed['channel_config'] == initial['channel_config']
    assert changed['ready'] == initial['ready'] and changed['issues'] == initial['issues']
    same = root.patch(path, json={'model_rpm_requirements': {'model-b': 700}}).json()
    assert same['version'] == changed['version'] and same['updated_at'] == changed['updated_at']
    audit = db.scalar(select(AuditEvent).where(AuditEvent.object_id == initial['id'],
        AuditEvent.action == 'upload_template.update').order_by(AuditEvent.created_at.desc()))
    assert 'model_rpm_requirements' in audit.summary['fields']
    assert audit.summary['version'] == changed['version']


def test_omitted_map_survives_other_edits_and_is_pruned_only_for_removed_models(db, login, catalog):
    root = login('root')
    initial = create(root, catalog, model_rpm_requirements={'model-a': 100, 'model-b': 200})
    path = '/api/upload-templates/' + initial['id']
    enabled = root.patch(path, json={'enabled': True}).json()
    assert enabled['model_rpm_requirements'] == initial['model_rpm_requirements']
    assert enabled['version'] == initial['version']
    renamed = root.patch(path, json={'name': 'Receiving demand'}).json()
    assert renamed['model_rpm_requirements'] == initial['model_rpm_requirements']
    reduced = root.patch(path, json={'models': ['model-b']}).json()
    assert reduced['model_rpm_requirements'] == {'model-b': 200}
    assert reduced['version'] == renamed['version'] + 1
    event = db.scalar(select(AuditEvent).where(AuditEvent.object_id == initial['id'],
        AuditEvent.action == 'upload_template.update').order_by(AuditEvent.created_at.desc()))
    assert {'models', 'model_rpm_requirements'} <= set(event.summary['fields'])
    added = root.patch(path, json={'models': ['model-a', 'model-b']}).json()
    assert added['model_rpm_requirements'] == {'model-b': 200}  # Newly selected has no invented demand.
    clear = root.patch(path, json={'model_rpm_requirements': {}}).json()
    assert clear['model_rpm_requirements'] == {} and clear['models'] == ['model-a', 'model-b']
    empty_draft = root.patch(path, json={'models': [], 'enabled': False}).json()
    assert empty_draft['model_rpm_requirements'] == {}


@pytest.mark.parametrize('value', [True, False, '1', '100', 1.0, 1.5, None, 0, -1, 1_000_001, [], {}])
def test_invalid_values_reject_create_and_patch_atomically(db, login, catalog, value):
    root = login('root')
    bad = {'model-a': value}
    response = root.post('/api/upload-templates', json=template_body(catalog, model_rpm_requirements=bad))
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(SiteUploadTemplate)) == 0
    initial = create(root, catalog, model_rpm_requirements={'model-a': 30})
    count = db.scalar(select(func.count()).select_from(AuditEvent))
    path = '/api/upload-templates/' + initial['id']
    assert root.patch(path, json={'name': 'must rollback', 'model_rpm_requirements': bad}).status_code == 422
    assert root.get(path).json() == initial
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == count


@pytest.mark.parametrize('bad', [None, [], True, 'model-a:100', {'outside-model': 100},
    {' model-a': 100}, {'model-a ': 100}, {'': 100}, {'model-a\n': 100},
    {'model-a': 100, ' model-a': 200}, {'model-a,model-b': 100}])
def test_invalid_map_or_model_keys_never_normalize_silently(db, login, catalog, bad):
    root = login('root')
    assert root.post('/api/upload-templates', json=template_body(catalog, model_rpm_requirements=bad)).status_code == 422
    initial = create(root, catalog, model_rpm_requirements={'model-a': 30})
    path = '/api/upload-templates/' + initial['id']
    assert root.patch(path, json={'model_rpm_requirements': bad}).status_code == 422
    assert root.get(path).json() == initial


def test_explicit_map_validates_against_final_models_without_pruning(db, login, catalog):
    root = login('root')
    initial = create(root, catalog, model_rpm_requirements={'model-a': 20})
    path = '/api/upload-templates/' + initial['id']
    assert root.patch(path, json={'models': ['model-b'], 'model_rpm_requirements': {'model-a': 20}}).status_code == 422
    assert root.get(path).json() == initial
    saved = root.patch(path, json={'models': [' model-b '], 'model_rpm_requirements': {'model-b': 40}})
    assert saved.status_code == 200
    assert saved.json()['models'] == ['model-b'] and saved.json()['model_rpm_requirements'] == {'model-b': 40}


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_model_requirements_remain_superadmin_only(db, login, catalog, role):
    root = login('root')
    initial = create(root, catalog, model_rpm_requirements={'model-a': 100})
    client = login(role)
    path = '/api/upload-templates/' + initial['id']
    assert client.get('/api/upload-templates').status_code == 403
    assert client.get(path).status_code == 403
    assert client.post('/api/upload-templates', json=template_body(catalog, index=1)).status_code == 403
    assert client.patch(path, json={'model_rpm_requirements': {}}).status_code == 403
    assert root.get(path).json()['model_rpm_requirements'] == {'model-a': 100}
    # Upload clients receive applicable model scope, not private demand settings.
    assert 'model_rpm_requirements' not in json.dumps(client.get('/api/uploads/options').json())


def test_demand_does_not_enter_frozen_configuration_or_remote_create(db, login, catalog, monkeypatch):
    root = login('root')
    template = create(root, catalog, model_rpm_requirements={'model-a': 876_543})
    member = login('user')
    request = submit(member, catalog)
    item = db.get(TaskItem, request['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    before = deepcopy(item.snapshot)
    assert 'model_rpm_requirements' not in template_config(db.get(SiteUploadTemplate, template['id']))
    assert 'model_rpm_requirements' not in json.dumps(before)
    assert 'model_rpm_requirements' not in json.dumps(dist.template_snapshot)
    assert before['rpm_enabled'] is False
    writes = []
    class Adapter:
        def __init__(self, site, before_write): self.before_write = before_write
        def find_unique_name(self, name): return None
        def create(self, **values):
            self.before_write()
            writes.append(values)
            return '940'
        def detail(self, remote_id):
            return {'id': 940, 'name': dist.remote_name, 'type': 1, 'status': 2,
                    'models': ','.join(before['models']), 'group': before['routing_group']}
    monkeypatch.setattr(worker, 'get_adapter', Adapter)
    worker.execute_item(db, item)
    assert len(writes) == 1 and 'model_rpm_requirements' not in json.dumps(writes)
    assert writes[0]['models'] == before['models'] and writes[0]['group'] == before['routing_group']
    assert db.get(Distribution, dist.id).status == 'disabled'


def migration(db):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'b841cd39e6a5_template_model_rpm_requirements.py'
    spec = importlib.util.spec_from_file_location('model_rpm_requirements_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'a730bc28d594'
    return module


@pytest.mark.parametrize('populated', [False, True])
def test_additive_migration_backfills_empty_map_without_changing_history(db, populated):
    if populated:
        sites = [site('rpm-migration-enabled'), site('rpm-migration-archived', archived=True)]
        db.add_all(sites)
        db.flush()
        add_history(db, sites)
        for index, row in enumerate(db.scalars(select(SiteUploadTemplate)), 1):
            row.models, row.version, row.enabled = ['old-model'], index + 5, index == 1
        db.flush()
    module = migration(db)
    module.downgrade()
    before = history_snapshot(db)
    for iteration in range(2):
        module.upgrade()
        after = history_snapshot(db)
        template_rows = after['site_upload_templates']
        assert all(row['model_rpm_requirements'] == {} for row in template_rows)
        after['site_upload_templates'] = [{key: value for key, value in row.items() if key != 'model_rpm_requirements'}
                                          for row in template_rows]
        assert after == before
        column = next(c for c in inspect(db.connection()).get_columns('site_upload_templates')
                      if c['name'] == 'model_rpm_requirements')
        assert column['nullable'] is False and '{}' in column['default']
        if iteration == 0:
            module.downgrade()
    db.commit()
    if populated:
        row = db.scalar(select(SiteUploadTemplate))
        # Exercise database default, independently of the ORM's Python default.
        db.execute(text('INSERT INTO site_upload_templates '
            '(id,site_id,category_id,format_id,variant,name,enabled,models,routing_group,remark,channel_config,version,created_at,updated_at) '
            "SELECT 'rpm-default-fixture',site_id,category_id,format_id,'rpm-default-variant',name,enabled,models,routing_group,remark,"
            'channel_config,version,created_at,updated_at FROM site_upload_templates WHERE id=:id'), {'id': row.id})
        db.commit()
        assert db.scalar(text("SELECT model_rpm_requirements FROM site_upload_templates WHERE id='rpm-default-fixture'")) == {}
