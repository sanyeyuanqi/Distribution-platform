"""Template business-service labels are read-only and share channel classification."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.bootstrap import seed_catalog
from app.db import uid
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.security import encrypt
from app.upload_templates import template_config
from sqlalchemy import select


@pytest.fixture
def service_templates(db, users, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail('Service projection tests must not contact upstreams')

    monkeypatch.setattr('app.adapters.silicon.safe_request', no_network)
    seed_catalog(db)
    categories = {row.family: row for row in db.scalars(select(Category))}
    formats = {(row.category_id, row.code): row for row in db.scalars(select(CredentialFormat))}
    site = Site(id=uid(), name='Service template fixture', prefix='svc', base_url='https://service.invalid',
                seller_user_id='1', token_encrypted=encrypt('fixture-private-token'), enabled=False)
    db.add(site)
    db.commit()
    return SimpleNamespace(categories=categories, formats=formats, site=site)


SERVICES = [
    ('AWS', 'newapi-33-aws-bedrock-v1', 'bedrock', 'AWS Bedrock', 'AWS Bedrock'),
    ('AWS', 'newapi-14-aws-claude-v1', 'aws_claude', 'AWS Claude 代理', 'Claude on AWS'),
    ('Anthropic', 'newapi-14-api-key-v1', '', 'Anthropic 官方', 'Anthropic API'),
    ('OpenAI', 'api_key-v1', '', 'OpenAI', 'OpenAI'),
    ('Azure', 'newapi-3-azure-gpt-v1', 'azure_gpt', 'Azure OpenAI', 'Azure OpenAI'),
    ('Azure', 'newapi-14-azure-claude-v1', 'azure_claude', 'Azure Claude', 'Azure Claude'),
    ('Google', 'newapi-24-api-key-v1', 'ai_studio_gemini', 'Google AI Studio', 'Google AI Studio'),
    ('Google', 'newapi-41-vertex-gemini-v1', 'vertex_gemini', 'Vertex AI Gemini', 'Vertex AI Gemini'),
    ('Google', 'newapi-41-vertex-claude-v1', 'vertex_claude', 'Vertex AI Claude', 'Vertex AI Claude'),
    ('OpenRouter', 'newapi-20-api-key-v1', '', 'OpenRouter', 'OpenRouter'),
    ('OpenCode', 'newapi-14-api-key-v1', '', 'OpenCode', 'OpenCode'),
]


def test_create_detail_patch_and_list_use_same_eleven_service_names(db, login, service_templates):
    case, root = service_templates, login('root')
    expected = {}
    for family, code, variant, name, name_en in SERVICES:
        category = case.categories[family]
        fmt = case.formats[(category.id, code)]
        response = root.post('/api/upload-templates', json={
            'site_id': case.site.id, 'category_id': category.id, 'format_id': fmt.id,
            'models': ['claude-opus-4-6'] if variant in ('vertex_claude', 'aws_claude') else ['gemini-test'],
            'enabled': False,
        })
        assert response.status_code == 201, response.text
        row = response.json()
        projection = {'service_variant': variant, 'service_name': name, 'service_name_en': name_en}
        assert {key: row[key] for key in projection} == projection
        assert row['variant'] == variant and row['category_id'] == category.id
        assert row['category_name'] == category.name and row['format_id'] == fmt.id
        assert root.get('/api/upload-templates/' + row['id']).json() == row
        before = deepcopy(template_config(db.get(SiteUploadTemplate, row['id'])))
        response = root.patch('/api/upload-templates/' + row['id'], json={'name': 'Updated ' + name})
        assert response.status_code == 200, response.text
        assert {key: response.json()[key] for key in projection} == projection
        assert response.json()['variant'] == variant
        expected[row['id']] = projection
        # Display labels do not enter frozen task/configuration comparisons.
        assert not any(key.startswith('service_') for key in before)

    listing = root.get('/api/upload-templates', params={'site_id': case.site.id}).json()
    assert listing['total'] == 11
    assert {row['id']: {key: row[key] for key in expected[row['id']]} for row in listing['items']} == expected
    for family, count in [('Azure', 2), ('Google', 3)]:
        filtered = root.get('/api/upload-templates', params={'category_id': case.categories[family].id}).json()
        assert filtered['total'] == count
        assert len({row['service_variant'] for row in filtered['items']}) == count


@pytest.mark.parametrize(('kind', 'models', 'variant', 'name'), [
    ('vertex_json', ['gemini-2.5-flash'], 'vertex_gemini', 'Vertex AI Gemini'),
    ('vertex_json', ['claude-opus-4-6'], 'vertex_claude', 'Vertex AI Claude'),
    ('vertex_json', ['gemini-2.5-flash', 'claude-opus-4-6'], 'vertex_legacy', 'Vertex AI（历史接入）'),
    ('vertex_api_key', ['claude-opus-4-6'], 'vertex_legacy', 'Vertex AI（历史接入）'),
])
def test_legacy_models_classify_display_without_mutating_stored_variant_or_configuration(
        db, login, service_templates, kind, models, variant, name):
    case = service_templates
    category = case.categories['Google']
    fmt = CredentialFormat(id=uid(), category_id=category.id, code='legacy-' + kind, version='1',
                           name='Legacy Vertex', schema_config={'type': kind, 'remote_type': 41})
    db.add(fmt)
    db.flush()
    row = SiteUploadTemplate(id=uid(), site_id=case.site.id, category_id=category.id, format_id=fmt.id,
                             variant='', models=models, enabled=False, version=7)
    db.add(row)
    db.commit()
    before = deepcopy(template_config(row))
    root = login('root')
    detail = root.get('/api/upload-templates/' + row.id)
    assert detail.status_code == 200, detail.text
    result = detail.json()
    assert result['service_variant'] == variant and result['service_name'] == name
    assert result['service_name_en']
    assert result['variant'] == '' and result['format_id'] == fmt.id and result['version'] == 7
    assert root.get('/api/upload-templates').json()['items'] == [result]
    db.refresh(row)
    assert row.variant == '' and row.models == models and template_config(row) == before
    assert 'fixture-private-token' not in str(result)


def test_existing_formats_expose_canonical_schema_for_new_service_selection(login, service_templates):
    root = login('root')
    formats = root.get('/api/formats').json()['items']
    by_key = {(row['category_id'], row['code']): row for row in formats}
    for family, code, variant, _, _ in SERVICES:
        row = by_key[(service_templates.categories[family].id, code)]
        schema = row['schema_config']
        if variant in ('azure_gpt', 'azure_claude', 'vertex_gemini', 'vertex_claude', 'aws_claude'):
            assert schema['type'] == variant
        elif variant == 'bedrock':
            assert schema['remote_type'] == 33
        elif variant == 'ai_studio_gemini':
            assert schema['remote_type'] == 24
