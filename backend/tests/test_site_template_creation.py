"""New sites receive independent disabled drafts from the installed catalog."""
from types import SimpleNamespace

import httpx
import pytest
from app.bootstrap import seed_catalog
from app.models import AuditEvent, Category, CredentialFormat, Site, SiteUploadTemplate
from app.routers import sites
from app.site_templates import create_site_templates
from sqlalchemy import select

EXPECTED_FORMATS = {
    ('AWS', 'bedrock'): 'newapi-33-aws-bedrock-v1',
    ('AWS', 'aws_claude'): 'newapi-14-aws-claude-v1',
    ('Anthropic', ''): 'newapi-14-api-key-v1',
    ('OpenAI', ''): 'api_key-v1',
    ('Azure', 'azure_gpt'): 'newapi-3-azure-gpt-v1',
    ('Azure', 'azure_claude'): 'newapi-14-azure-claude-v1',
    ('Google', 'ai_studio_gemini'): 'newapi-24-api-key-v1',
    ('Google', 'vertex_gemini'): 'newapi-41-vertex-gemini-v1',
    ('Google', 'vertex_claude'): 'newapi-41-vertex-claude-v1',
    ('OpenRouter', ''): 'newapi-20-api-key-v1',
    ('OpenCode', ''): 'newapi-14-api-key-v1',
}


@pytest.fixture(autouse=True)
def mock_site_api(monkeypatch):
    state = SimpleNamespace(failed=False, calls=[])

    def verify(row):
        state.calls.append(row.base_url)
        if state.failed:
            raise RuntimeError('Fixture verification failure')
        return {'verified_version': 'v0.13.2', 'capabilities': {
            'read': 'supported', 'create': 'supported', 'can_write': True}}

    def forbid_network(*args, **kwargs):
        pytest.fail('Site template creation must not make an unstubbed remote request')

    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    monkeypatch.setattr(sites, 'get_adapter', lambda row: SimpleNamespace(verify=lambda: verify(row)))
    # TestClient uses its own ASGI transport, so this only blocks external HTTP.
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbid_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbid_network)
    return state


@pytest.fixture
def catalog(db):
    seed_catalog(db)
    db.commit()
    return {row.family: row for row in db.scalars(select(Category))}


def site_body(label='fixture', **values):
    return {'name': label, 'prefix': label, 'base_url': f'https://{label}.invalid',
            'seller_user_id': '71', 'token': 'fixture-token', **values}


def create(client, label='fixture', **values):
    response = client.post('/api/sites', json=site_body(label, **values))
    assert response.status_code == 201, response.text
    return response.json()


def template_rows(db, site_id):
    db.expire_all()
    return list(db.scalars(select(SiteUploadTemplate).where(SiteUploadTemplate.site_id == site_id)
                           .order_by(SiteUploadTemplate.display_id)))


def snapshot(rows):
    return [{column.name: getattr(row, column.name) for column in SiteUploadTemplate.__table__.columns}
            for row in rows]


@pytest.mark.parametrize('requested_enabled', [False, True])
def test_creating_site_generates_all_catalog_variants_as_linked_disabled_drafts(
        db, login, users, catalog, requested_enabled, mock_site_api):
    root = login('root')
    result = create(root, enabled=requested_enabled)
    assert result['enabled'] is requested_enabled and result['distribution_ready'] is True
    assert result['distribution_issues'] == []
    assert result['enabled_template_count'] == 0
    rows = template_rows(db, result['id'])
    assert len(rows) == 11
    assert len({row.category_id for row in rows}) == 7
    assert len({row.display_id for row in rows}) == 11
    actual_formats = {}
    for row in rows:
        category, fmt = db.get(Category, row.category_id), db.get(CredentialFormat, row.format_id)
        assert row.site_id == result['id'] and fmt.category_id == category.id
        actual_formats[category.family, row.variant] = fmt.code
        assert row.name.endswith(' · fixture')
        assert row.enabled is False and row.models == []
        assert row.routing_group == 'default' and row.channel_config == {'status': 2}
        assert row.model_rpm_requirements == row.model_tpm_requirements == row.model_mapping == {}
        assert row.proxy_encrypted is None and row.remark == '' and row.version == 1
    assert actual_formats == EXPECTED_FORMATS
    assert mock_site_api.calls == ['https://fixture.invalid']
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == 'upload_template.create')))
    assert {event.object_id for event in events} == {row.id for row in rows}
    for event in events:
        assert event.actor_id == users['root'].id and event.actor_role == 'superadmin'
        assert event.summary['site_id'] == result['id']
        assert event.summary['source'] == 'site.create' and event.summary['enabled'] is False
    site_event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'site.create'))
    assert site_event.object_id == result['id'] and site_event.summary['created_template_count'] == 11
    visible = root.get('/api/upload-templates', params={'site_id': result['id']}).json()
    assert visible['total'] == 11
    assert {row['id'] for row in visible['items']} == {row.id for row in rows}
    assert all(row['enabled'] is False and row['ready'] is False for row in visible['items'])


def test_failed_site_verification_still_saves_disabled_local_drafts(db, login, catalog, mock_site_api):
    mock_site_api.failed = True
    result = create(login('root'), enabled=True)
    assert result['enabled'] is False and result['verified_at'] is None
    assert result['distribution_ready'] is False
    assert result['verification_error'] is not None
    rows = template_rows(db, result['id'])
    assert len(rows) == 11 and all(not row.enabled and row.models == [] for row in rows)
    assert mock_site_api.calls == ['https://fixture.invalid']


def test_new_site_drafts_do_not_copy_existing_site_receiving_configuration(db, login, catalog):
    original = Site(name='Original', prefix='original', base_url='https://original.invalid',
                    seller_user_id='71', token_encrypted='fixture-ciphertext', routing_group='other-group')
    db.add(original)
    db.flush()
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == catalog['OpenAI'].id))
    fmt.default_models = ['unrelated-default-model']
    existing = SiteUploadTemplate(site_id=original.id, category_id=fmt.category_id, format_id=fmt.id,
        name='Manual template', enabled=True, models=['other-site-model'], routing_group='other-group',
        model_rpm_requirements={'other-site-model': 300}, model_tpm_requirements={'other-site-model': 30000},
        model_mapping={'other-site-model': 'other-alias'}, channel_config={'status': 1},
        proxy_encrypted='fixture-proxy-ciphertext', remark='Existing configuration', version=7)
    db.add(existing)
    db.commit()
    before = snapshot([existing])
    result = create(login('root'), routing_group='receiving-group')
    rows = template_rows(db, result['id'])
    assert len(rows) == 11
    assert all(row.routing_group == 'receiving-group' and row.models == [] and not row.enabled for row in rows)
    assert all(row.channel_config == {'status': 2} and row.proxy_encrypted is None for row in rows)
    assert all(row.model_rpm_requirements == row.model_tpm_requirements == row.model_mapping == {} for row in rows)
    assert snapshot(template_rows(db, original.id)) == before


def test_repeated_creation_preserves_manually_edited_templates_and_audit_count(db, login, users, catalog):
    root = login('root')
    result = create(root)
    row = next(row for row in template_rows(db, result['id']) if row.category_id == catalog['OpenAI'].id)
    patched = root.patch('/api/upload-templates/' + row.id, json={
        'name': 'Configured later', 'models': ['fixture-model'], 'routing_group': 'manual-group',
        'model_rpm_requirements': {'fixture-model': 42}, 'channel_config': {'status': 1}, 'remark': 'Keep me'})
    assert patched.status_code == 200, patched.text
    before = snapshot(template_rows(db, result['id']))
    original_audits = set(db.scalars(select(AuditEvent.id)))
    site = db.get(Site, result['id'])
    assert create_site_templates(db, site, users['root']) == []
    assert create_site_templates(db, site, users['root']) == []
    db.commit()
    assert snapshot(template_rows(db, site.id)) == before
    assert set(db.scalars(select(AuditEvent.id))) == original_audits


def test_site_edit_and_verify_do_not_recreate_a_deleted_template(db, login, catalog):
    root = login('root')
    result = create(root)
    rows = template_rows(db, result['id'])
    deleted_id = rows[0].id
    assert root.delete('/api/upload-templates/' + deleted_id).status_code == 200
    remaining = {row.id for row in rows[1:]}
    assert root.patch('/api/sites/' + result['id'], json={'name': 'Renamed', 'token': 'replacement-fixture-token'}).status_code == 200
    assert root.post('/api/sites/' + result['id'] + '/verify').status_code == 200
    assert {row.id for row in template_rows(db, result['id'])} == remaining
    assert db.get(SiteUploadTemplate, deleted_id) is None


@pytest.mark.parametrize('conflict', ['prefix', 'base_url'])
def test_conflicting_site_creation_rolls_back_without_orphan_templates(db, login, catalog, conflict):
    root = login('root')
    original = create(root, 'original')
    before = snapshot(template_rows(db, original['id']))
    original_audits = set(db.scalars(select(AuditEvent.id)))
    body = site_body('duplicate')
    body[conflict] = original[conflict]
    response = root.post('/api/sites', json=body)
    assert response.status_code == 409, response.text
    db.expire_all()
    assert list(db.scalars(select(Site.id))) == [original['id']]
    assert snapshot(template_rows(db, original['id'])) == before
    assert len(list(db.scalars(select(SiteUploadTemplate.id)))) == 11
    assert set(db.scalars(select(AuditEvent.id))) == original_audits


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_non_superadmins_cannot_create_sites_or_automatic_templates(db, login, catalog, role, mock_site_api):
    response = login(role).post('/api/sites', json=site_body())
    assert response.status_code == 403
    assert list(db.scalars(select(Site.id))) == []
    assert list(db.scalars(select(SiteUploadTemplate.id))) == []
    assert list(db.scalars(select(AuditEvent.id).where(AuditEvent.action == 'upload_template.create'))) == []
    assert mock_site_api.calls == []


def test_inactive_or_non_source_owned_catalog_definitions_are_not_provisioned(db, login, catalog):
    catalog['Google'].active = False
    azure = db.scalar(select(CredentialFormat).where(CredentialFormat.code == 'newapi-3-azure-gpt-v1'))
    azure.enabled = False
    openai = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == catalog['OpenAI'].id))
    openai.schema_config = {'type': 'api_key', 'remote_type': 999}
    custom_category = Category(name='Custom provider', family='Custom', active=True)
    db.add(custom_category)
    db.flush()
    db.add_all([
        CredentialFormat(category_id=catalog['OpenAI'].id, name='Unsupported custom format', code='custom-key-v1',
                         enabled=True, schema_config={'type': 'api_key', 'remote_type': 1}),
        CredentialFormat(category_id=custom_category.id, name='Outside fixed catalog', code='api_key-v1',
                         enabled=True, schema_config={'type': 'api_key', 'remote_type': 1}),
    ])
    db.commit()
    result = create(login('root'))
    rows = template_rows(db, result['id'])
    expected = {key for key in EXPECTED_FORMATS if key[0] not in ('Google', 'OpenAI') and key != ('Azure', 'azure_gpt')}
    assert {(db.get(Category, row.category_id).family, row.variant) for row in rows} == expected
    assert len(rows) == 6


def test_legacy_empty_site_routing_group_uses_default_for_new_drafts(db, users, catalog):
    site = Site(name='Legacy', prefix='legacy', base_url='https://legacy.invalid', seller_user_id='71',
                token_encrypted='fixture-ciphertext', routing_group='')
    db.add(site)
    db.flush()
    rows = create_site_templates(db, site, users['root'])
    db.commit()
    assert len(rows) == 11 and all(row.routing_group == 'default' for row in rows)
