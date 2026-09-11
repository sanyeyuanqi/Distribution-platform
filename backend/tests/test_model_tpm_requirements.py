"""Token demand persists independently of request demand and upstream limits."""
# ruff: noqa: F811
import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models import SiteUploadTemplate
from app.models_channels import Distribution, TaskItem
from app.upload_templates import template_config
from sqlalchemy import inspect, select
from test_model_rpm_requirements import create
from test_site_display_ids import add_history, history_snapshot, site
from test_upload_templates import (  # noqa: F401
    catalog,
    no_network,
    submit,
    template_body,
)


def test_rpm_and_tpm_roundtrip_and_partial_updates_remain_independent(db, login, catalog):
    root = login('root')
    original = create(root, catalog, model_rpm_requirements={'model-a': 300},
                      model_tpm_requirements={'model-a': 2_000_000, 'model-b': 1_000_000_000})
    path = '/api/upload-templates/' + original['id']
    assert root.get(path).json() == original
    assert root.get('/api/upload-templates').json()['items'][0] == original

    def patch(values):
        response = root.patch(path, json=values)
        assert response.status_code == 200, response.text
        return response.json()

    rpm_changed = patch({'model_rpm_requirements': {'model-a': 500}})
    assert rpm_changed['model_tpm_requirements'] == original['model_tpm_requirements']
    assert rpm_changed['version'] == original['version'] + 1
    tpm_changed = patch({'model_tpm_requirements': {'model-a': 7_000_000}})
    assert tpm_changed['model_rpm_requirements'] == {'model-a': 500}
    assert tpm_changed['model_tpm_requirements'] == {'model-a': 7_000_000}
    assert tpm_changed['version'] == rpm_changed['version'] + 1
    assert patch({'model_tpm_requirements': {'model-a': 7_000_000}})['version'] == tpm_changed['version']
    assert patch({'name': 'Same demand'})['model_tpm_requirements'] == {'model-a': 7_000_000}
    cleared_rpm = patch({'model_rpm_requirements': {}})
    assert cleared_rpm['model_tpm_requirements'] == {'model-a': 7_000_000}
    patch({'model_rpm_requirements': {'model-a': 200}})
    cleared_tpm = patch({'model_tpm_requirements': {}})
    assert cleared_tpm['model_rpm_requirements'] == {'model-a': 200}
    assert cleared_tpm['model_tpm_requirements'] == {}
    legacy = create(root, catalog, index=1, model_rpm_requirements={'model-b': 99})
    assert legacy['model_tpm_requirements'] == {}


def test_model_changes_prune_both_maps_and_explicit_invalid_map_rolls_back(db, login, catalog):
    root = login('root')
    original = create(root, catalog, model_rpm_requirements={'model-a': 100, 'model-b': 200},
                      model_tpm_requirements={'model-a': 3_000_000, 'model-b': 4_000_000})
    path = '/api/upload-templates/' + original['id']
    bad = root.patch(path, json={'models': ['model-b'], 'model_tpm_requirements': {'model-a': 5}})
    assert bad.status_code == 422
    assert root.get(path).json() == original
    reduced = root.patch(path, json={'models': ['model-b']}).json()
    assert reduced['model_rpm_requirements'] == {'model-b': 200}
    assert reduced['model_tpm_requirements'] == {'model-b': 4_000_000}
    restored = root.patch(path, json={'models': ['model-a', 'model-b'],
                                    'model_tpm_requirements': {'model-a': 55}}).json()
    assert restored['model_rpm_requirements'] == {'model-b': 200}
    assert restored['model_tpm_requirements'] == {'model-a': 55}
    empty = root.patch(path, json={'models': [], 'enabled': False}).json()
    assert empty['model_tpm_requirements'] == empty['model_rpm_requirements'] == {}


@pytest.mark.parametrize('bad', [
    {'model-a': True}, {'model-a': '10'}, {'model-a': 1.0}, {'model-a': 1.5},
    {'model-a': None}, {'model-a': 0}, {'model-a': -1}, {'model-a': 1_000_000_001},
    {'unselected': 100}, {' model-a': 100}, None, [],
])
def test_invalid_tpm_cannot_create_or_overwrite_existing_rpm(db, login, catalog, bad):
    root = login('root')
    rejected = root.post('/api/upload-templates', json=template_body(catalog, model_tpm_requirements=bad))
    assert rejected.status_code == 422
    original = create(root, catalog, model_rpm_requirements={'model-a': 10}, model_tpm_requirements={'model-a': 100})
    path = '/api/upload-templates/' + original['id']
    assert root.patch(path, json={'model_rpm_requirements': {}, 'model_tpm_requirements': bad}).status_code == 422
    assert root.get(path).json() == original


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_tpm_remains_superadmin_metadata_and_is_not_sent_to_distribution(db, login, catalog, role):
    root = login('root')
    original = create(root, catalog, model_tpm_requirements={'model-a': 8_000_000})
    client = login(role)
    path = '/api/upload-templates/' + original['id']
    assert client.patch(path, json={'model_tpm_requirements': {}}).status_code == 403
    assert client.post('/api/upload-templates', json=template_body(catalog, index=1,
                        model_tpm_requirements={'model-b': 10})).status_code == 403
    assert client.get(path).status_code == 403
    assert 'model_tpm_requirements' not in json.dumps(client.get('/api/uploads/options').json())
    request = submit(client, catalog)
    item = db.get(TaskItem, request['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    assert 'model_tpm_requirements' not in json.dumps(item.snapshot)
    assert 'model_tpm_requirements' not in json.dumps(dist.template_snapshot)
    assert 'model_tpm_requirements' not in template_config(db.get(SiteUploadTemplate, original['id']))
    assert item.snapshot['rpm_enabled'] is False


@pytest.mark.parametrize('populated', [False, True])
def test_tpm_migration_preserves_configured_rpm_and_existing_history(db, populated):
    if populated:
        sites = [site('tpm-migration')]
        db.add_all(sites)
        db.flush()
        add_history(db, sites)
        for row in db.scalars(select(SiteUploadTemplate)):
            row.models, row.model_rpm_requirements = ['old-model'], {'old-model': 750}
        db.flush()
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'c952de4af7b6_template_model_tpm_requirements.py'
    spec = importlib.util.spec_from_file_location('tpm_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'b841cd39e6a5'
    module.downgrade()
    before = history_snapshot(db)
    module.upgrade()
    after = history_snapshot(db)
    for row in after['site_upload_templates']:
        assert row.pop('model_tpm_requirements') == {}
    assert after == before
    column = next(c for c in inspect(db.connection()).get_columns('site_upload_templates')
                  if c['name'] == 'model_tpm_requirements')
    assert column['nullable'] is False and '{}' in column['default']
    db.commit()
