"""Explicit template aliases preserve source scope and reach the frozen wire DTO."""
# ruff: noqa: F811
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app import worker
from app.adapters.channel_config import create_payload, validate_readback
from app.models import AuditEvent, SiteUploadTemplate
from app.models_channels import Distribution, Task, TaskItem
from app.routers.upload_templates import TemplateCreate, TemplatePatch
from app.upload_templates import effective_template, template_config
from pydantic import ValidationError
from sqlalchemy import func, inspect, select, text
from test_site_display_ids import add_history, history_snapshot, site
from test_upload_templates import (  # noqa: F401
    catalog,
    configure_claude_mapping_catalog,
    no_network,
    submit,
    template_body,
    upload,
)


@pytest.fixture
def mapping_catalog(db, catalog):
    for target in catalog[2]:
        target.capabilities = {**target.capabilities, 'channel_config': 'supported', 'can_edit_routing': True}
    db.commit()
    return catalog


def create(root, catalog, **changes):
    body = template_body(catalog, **{'models': ['model-a', 'model-b'], **changes})
    response = root.post('/api/upload-templates', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_mapping_crud_is_independent_of_demands_and_legacy_config(db, login, mapping_catalog):
    root = login('root')
    original = create(root, mapping_catalog, model_mapping={'model-a': 'upstream/custom-v2'},
        model_rpm_requirements={'model-a': 100}, model_tpm_requirements={'model-a': 2_000_000},
        channel_config={'status': 2, 'model_mapping': {'model-b': 'legacy-must-be-stripped'}, 'priority': 9})
    path = '/api/upload-templates/' + original['id']
    assert original['channel_config'] == {'status': 2}
    assert root.get(path).json() == original
    assert root.get('/api/upload-templates').json()['items'][0] == original
    renamed = root.patch(path, json={'name': 'Custom source alias'}).json()
    assert renamed['model_mapping'] == original['model_mapping']
    replaced = root.patch(path, json={'model_mapping': {'model-b': 'upstream/other'}}).json()
    assert replaced['model_mapping'] == {'model-b': 'upstream/other'}
    assert replaced['version'] == renamed['version'] + 1
    same = root.patch(path, json={'model_mapping': replaced['model_mapping']}).json()
    assert (same['version'], same['updated_at']) == (replaced['version'], replaced['updated_at'])
    cleared = root.patch(path, json={'model_mapping': {}}).json()
    assert cleared['model_mapping'] == {}
    for key in ('model_rpm_requirements', 'model_tpm_requirements', 'models', 'channel_config', 'ready', 'issues'):
        assert cleared[key] == original[key]
    event = db.scalar(select(AuditEvent).where(AuditEvent.object_id == original['id'],
        AuditEvent.action == 'upload_template.update').order_by(AuditEvent.created_at.desc()))
    assert 'model_mapping' in event.summary['fields']
    assert event.summary['version'] == cleared['version']


def test_models_prune_omitted_mapping_but_explicit_unselected_source_is_atomic(db, login, mapping_catalog):
    root = login('root')
    original = create(root, mapping_catalog, model_mapping={'model-a': 'upstream-a', 'model-b': 'upstream-b'},
        model_rpm_requirements={'model-a': 100, 'model-b': 200},
        model_tpm_requirements={'model-a': 300, 'model-b': 400})
    path = '/api/upload-templates/' + original['id']
    rejected = root.patch(path, json={'models': ['model-b'], 'model_mapping': {'model-a': 'upstream'}})
    assert rejected.status_code == 422 and root.get(path).json() == original
    reduced = root.patch(path, json={'models': [' model-b ']}).json()
    assert reduced['models'] == ['model-b'] and reduced['model_mapping'] == {'model-b': 'upstream-b'}
    assert reduced['model_rpm_requirements'] == {'model-b': 200}
    assert reduced['model_tpm_requirements'] == {'model-b': 400}
    assert reduced['version'] == original['version'] + 1
    restored = root.patch(path, json={'models': ['model-a', 'model-b']}).json()
    assert restored['model_mapping'] == {'model-b': 'upstream-b'}
    assert root.patch(path, json={'model_tpm_requirements': {}}).json()['model_mapping'] == restored['model_mapping']
    empty = root.patch(path, json={'enabled': False, 'models': []}).json()
    assert empty['model_mapping'] == empty['model_rpm_requirements'] == empty['model_tpm_requirements'] == {}


@pytest.mark.parametrize('entrypoint', ['create', 'patch'])
def test_new_mapping_source_joins_models_atomically_and_explicit_deletion_survives_reopen(db, login, mapping_catalog, entrypoint):
    root = login('root')
    site = mapping_catalog[2][0]
    site.capabilities = {**site.capabilities, 'custom_models': True}
    db.commit()
    original = None
    if entrypoint == 'patch':
        original = create(root, mapping_catalog, models=['model-a'], model_mapping={'model-a': 'upstream-a'},
                          model_rpm_requirements={'model-a': 10}, model_tpm_requirements={'model-a': 1000})
    added_values = {'models': ['model-a', 'new-source'],
        'model_mapping': {'model-a': 'upstream-a', 'new-source': 'model-b'},
        'model_rpm_requirements': {'model-a': 10, 'new-source': 20},
        'model_tpm_requirements': {'model-a': 1000, 'new-source': 2000}}
    if original:
        response = root.patch('/api/upload-templates/' + original['id'], json=added_values)
        assert response.status_code == 200, response.text
        added = response.json()
        assert added['version'] == original['version'] + 1
    else:
        added = create(root, mapping_catalog, **added_values)
    path = '/api/upload-templates/' + added['id']
    reopened = root.get(path).json()
    for field, expected in added_values.items():
        assert added[field] == reopened[field] == expected
    assert added['ready'] and added['issues'] == []
    # The frontend submits the final accepted list and all three pruned maps.
    removed_values = {'models': ['model-a'], 'model_mapping': {'model-a': 'upstream-a'},
        'model_rpm_requirements': {'model-a': 10}, 'model_tpm_requirements': {'model-a': 1000}}
    response = root.patch(path, json=removed_values)
    assert response.status_code == 200, response.text
    removed = response.json()
    assert removed['version'] == added['version'] + 1
    assert root.get(path).json() == removed
    assert root.get('/api/upload-templates').json()['items'][0] == removed
    for field, expected in removed_values.items():
        assert removed[field] == expected


def test_adding_mapped_source_keeps_existing_site_model_gate_and_rolls_back_all_fields(db, login, mapping_catalog):
    root = login('root')
    original = create(root, mapping_catalog, models=['model-a'], model_rpm_requirements={'model-a': 10},
                      model_tpm_requirements={'model-a': 1000})
    path = '/api/upload-templates/' + original['id']
    added_values = {'models': ['model-a', 'new-source'], 'model_mapping': {'new-source': 'model-b'},
                   'model_rpm_requirements': {'new-source': 20}, 'model_tpm_requirements': {'new-source': 2000}}
    rejected = root.patch(path, json=added_values)
    assert rejected.status_code == 422 and '不支持的模型' in rejected.text
    assert root.get(path).json() == original
    # Saving a draft is supported; a target alias does not grant custom-model capability.
    draft = root.patch(path, json={**added_values, 'enabled': False})
    assert draft.status_code == 200, draft.text
    assert draft.json()['models'] == added_values['models']
    assert draft.json()['model_mapping'] == added_values['model_mapping']
    assert root.patch(path, json={'enabled': True}).status_code == 422
    assert root.get(path).json() == draft.json()


@pytest.mark.parametrize('bad', [None, [], True, 'model-a:target', {'outside': 'target'},
    {'model-a': None}, {'model-a': True}, {'model-a': 3}, {'model-a': 1.2}, {'model-a': []},
    {'model-a': {}}, {' model-a': 'target'}, {'model-a ': 'target'},
    {'model-a': 'target', ' model-a': 'other'}])
def test_invalid_mapping_types_or_sources_never_overwrite_saved_state(db, login, mapping_catalog, bad):
    root = login('root')
    assert root.post('/api/upload-templates', json=template_body(mapping_catalog, model_mapping=bad)).status_code == 422
    assert db.scalar(select(func.count()).select_from(SiteUploadTemplate)) == 0
    original = create(root, mapping_catalog, model_mapping={'model-a': 'upstream'})
    path = '/api/upload-templates/' + original['id']
    count = db.scalar(select(func.count()).select_from(AuditEvent))
    assert root.patch(path, json={'name': 'must rollback', 'model_mapping': bad}).status_code == 422
    assert root.get(path).json() == original
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == count


@pytest.mark.parametrize('model', ['', ' spaced', 'spaced ', 'two,models', 'a\x00b', 'a\tb', 'a\nb', 'a\x7fb',
    'x' * 201, '模' * 86])
@pytest.mark.parametrize('side', ['source', 'target'])
def test_mapping_ids_are_exact_bounded_and_control_free(model, side):
    mapping = {model: 'target'} if side == 'source' else {'model-a': model}
    with pytest.raises(ValidationError):
        TemplatePatch(model_mapping=mapping)
    with pytest.raises(ValidationError):
        TemplateCreate(site_id='site', category_id='category', format_id='format', model_mapping=mapping)


def test_mapping_limits_accept_200_items_and_exact_unicode_byte_boundary():
    for model in ('x' * 200, '模' * 85):
        assert TemplatePatch(model_mapping={model: model}).model_mapping == {model: model}
    mapping = {f'model-{index}': f'upstream/{index}' for index in range(200)}
    assert len(TemplatePatch(model_mapping=mapping).model_mapping) == 200
    with pytest.raises(ValidationError):
        TemplatePatch(model_mapping={**mapping, 'model-201': 'upstream'})
    with pytest.raises(ValidationError):
        TemplatePatch(model_mapping={1: 'target'})


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_mapping_write_and_management_remain_superadmin_only(db, login, mapping_catalog, role):
    root = login('root')
    original = create(root, mapping_catalog, model_mapping={'model-a': 'upstream'})
    path = '/api/upload-templates/' + original['id']
    client = login(role)
    assert client.get(path).status_code == 403
    assert client.get('/api/upload-templates').status_code == 403
    assert client.patch(path, json={'model_mapping': {}}).status_code == 403
    assert client.post('/api/upload-templates', json=template_body(mapping_catalog, index=1,
        model_mapping={'model-b': 'other'})).status_code == 403
    assert root.get(path).json() == original


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode', 'Google'])
def test_custom_mapping_overrides_automatic_aliases_only_after_intersection(family):
    haiku, opus = 'claude-haiku-4-5-20251001', 'claude-opus-4-6'
    template = SimpleNamespace(models=[haiku, opus], channel_config={'status': 2},
        model_mapping={haiku: 'provider/custom', opus: 'provider/unused'})
    category = SimpleNamespace(family=family)
    fmt = SimpleNamespace(schema_config={'type': 'vertex_claude', 'remote_type': 41}) if family == 'Google' else None
    result = effective_template(template, {'models': [haiku, 'key-only']}, category=category, fmt=fmt)
    assert result['models'] == [haiku]
    assert result['channel_config']['model_mapping'] == {haiku: 'provider/custom'}
    assert template.model_mapping[opus] == 'provider/unused'
    template.model_mapping = {}
    automatic = effective_template(template, {'models': [haiku]}, category=category, fmt=fmt)
    assert automatic['channel_config']['model_mapping'][haiku] != 'provider/custom'


def test_mapping_is_frozen_and_sent_as_wire_mapping_without_expanding_models(db, login, mapping_catalog, monkeypatch):
    root = login('root')
    original = create(root, mapping_catalog, model_mapping={'model-a': 'upstream-a', 'model-b': 'upstream-b'})
    task = submit(login('user'), mapping_catalog, models=['model-b', 'key-only'])
    item = db.get(TaskItem, task['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    assert item.snapshot['models'] == ['model-b']
    assert item.snapshot['template_config']['model_mapping'] == original['model_mapping']
    assert item.snapshot['channel_config']['model_mapping'] == {'model-b': 'upstream-b'}
    assert dist.template_snapshot['model_mapping'] == original['model_mapping']
    sent = []
    options = {'supported_types': {1}, 'routing': True, 'supports_proxy': False,
               'supports_rpm': False, 'object_settings': True}

    class Adapter:
        def __init__(self, site, before_write): self.before_write = before_write
        def find_unique_name(self, name): return None
        def create(self, **values):
            self.before_write()
            sent.append(create_payload(**values, **options))
            return '997'
        def detail(self, remote_id): return {'id': 997, **sent[0]}
        def validate_created_config(self, remote, config, proxy, channel_type):
            validate_readback(remote, config, proxy, channel_type, **options)

    monkeypatch.setattr(worker, 'get_adapter', Adapter)
    worker.execute_item(db, item)
    assert len(sent) == 1
    assert sent[0]['models'] == 'model-b' and json.loads(sent[0]['model_mapping']) == {'model-b': 'upstream-b'}
    assert sent[0]['type'] == 1 and dist.models == ['model-b']


def test_changed_mapping_blocks_old_worker_and_repeat_upload_but_reprepare_freezes_new_map(db, login, mapping_catalog, monkeypatch):
    root, client = login('root'), login('user')
    original = create(root, mapping_catalog, model_mapping={'model-a': 'old-target'})
    result = submit(client, mapping_catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    old_snapshot = deepcopy(item.snapshot)
    revised = root.patch('/api/upload-templates/' + original['id'], json={'model_mapping': {'model-a': 'new-target'}})
    assert revised.status_code == 200 and revised.json()['version'] == original['version'] + 1
    class NoRemote:
        def find_unique_name(self, name): pytest.fail('Stale mapping must stop before remote access')
        def create(self, **values): pytest.fail('Stale mapping must stop before remote access')
    monkeypatch.setattr(worker, 'get_adapter', lambda *args, **kwargs: NoRemote())
    with pytest.raises(worker.WriteStopped, match='模板'):
        worker.execute_item(db, item)
    assert not item.remote_write_attempted and item.snapshot == old_snapshot
    item.status = 'failed'
    db.get(Task, item.task_id).status = 'failed'
    db.commit()
    preview = client.post('/api/uploads/simple-preview', json=upload(mapping_catalog)).json()
    assert not preview['can_submit'] and preview['rows'][0]['status'] == 'conflict'
    refreshed = client.post('/api/tasks/' + item.task_id + '/reprepare-templates')
    assert refreshed.status_code == 200, refreshed.text
    fresh = db.get(TaskItem, refreshed.json()['items'][0]['id'])
    assert fresh.snapshot['template_config']['model_mapping'] == {'model-a': 'new-target'}
    assert fresh.snapshot['channel_config']['model_mapping'] == {'model-a': 'new-target'}
    assert db.get(TaskItem, item.id, populate_existing=True).snapshot == old_snapshot


def test_default_mapping_preserves_legacy_frozen_shape_and_does_not_restore_advanced_map(db, login, mapping_catalog):
    root = login('root')
    original = create(root, mapping_catalog, channel_config={'model_mapping': {'model-a': 'ignored'}})
    template = db.get(SiteUploadTemplate, original['id'])
    assert original['model_mapping'] == {} and original['channel_config'] == {'status': 2}
    legacy = {key: getattr(template, key) for key in (
        'id', 'version', 'site_id', 'category_id', 'format_id', 'models', 'routing_group', 'remark')}
    legacy.update(channel_config=template.channel_config, proxy_fingerprint=None)
    assert template_config(template) == legacy
    result = submit(login('user'), mapping_catalog)
    item = db.get(TaskItem, result['items'][0]['id'])
    assert item.snapshot['template_config'] == legacy
    assert item.snapshot['channel_config']['model_mapping'] == {}
    worker.assert_execution(db, db.get(Task, item.task_id), item, write=True)


@pytest.mark.parametrize('populated', [False, True])
def test_mapping_migration_preserves_rpm_tpm_and_all_existing_history(db, populated):
    if populated:
        sites = [site('mapping-current'), site('mapping-archived', archived=True)]
        db.add_all(sites)
        db.flush()
        add_history(db, sites)
        for row in db.scalars(select(SiteUploadTemplate)):
            row.models = ['old-model']
            row.model_rpm_requirements, row.model_tpm_requirements = {'old-model': 123}, {'old-model': 2_000_000}
        db.flush()
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'da63ef5b08c7_template_model_mapping.py'
    spec = importlib.util.spec_from_file_location('template_mapping_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'c952de4af7b6'
    module.downgrade()
    before = history_snapshot(db)
    for iteration in range(2):
        module.upgrade()
        after = history_snapshot(db)
        for row in after['site_upload_templates']:
            assert row.pop('model_mapping') == {}
        assert after == before
        column = next(c for c in inspect(db.connection()).get_columns('site_upload_templates') if c['name'] == 'model_mapping')
        assert column['nullable'] is False and '{}' in column['default']
        if not iteration:
            module.downgrade()
    db.commit()
    if populated:
        row = db.scalar(select(SiteUploadTemplate))
        db.execute(text('INSERT INTO site_upload_templates '
            '(id,site_id,category_id,format_id,variant,name,enabled,models,routing_group,remark,channel_config,version,created_at,updated_at) '
            "SELECT 'mapping-default-fixture',site_id,category_id,format_id,'mapping-default-variant',name,enabled,models,routing_group,remark,"
            'channel_config,version,created_at,updated_at FROM site_upload_templates WHERE id=:id'), {'id': row.id})
        db.commit()
        assert db.scalar(text("SELECT model_mapping FROM site_upload_templates WHERE id='mapping-default-fixture'")) == {}
