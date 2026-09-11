"""Pure selector serialization checks; no database, credentials or remote calls."""
from types import SimpleNamespace

import pytest
from app.catalog_policy import get_catalog_format_specs
from app.google_services import CLAUDE9
from app.models import Category, CredentialFormat, Site, SiteUploadTemplate
from app.newapi_formats import format_default_models
from app.routers import catalog
from app.upload_templates import _resolve_templates, effective_template

EXPECTED_CLAUDE_MODELS = [
    'claude-haiku-4-5-20251001',
    'claude-opus-4-6',
    'claude-opus-4-7',
    'claude-opus-4-8',
    'claude-sonnet-5',
    'claude-fable-5',
    'claude-opus-5',
    'claude-sonnet-4-6',
    'claude-fable-5-1',
]


class ScalarRows(list):
    def all(self):
        return list(self)


class CatalogSession:
    """Only the catalog read operations, backed by in-memory definitions."""

    def __init__(self, family):
        self.category = SimpleNamespace(id=family, family=family, active=True)
        self.formats = [SimpleNamespace(
            id=spec['code'], category_id=family, code=spec['code'], name=spec['name'],
            version=spec['version'], schema_config=spec['schema_config'], enabled=True,
            default_models=['historical-stored-default'],
        ) for spec in get_catalog_format_specs() if spec['family'] == family]

    def get(self, model, identifier):
        assert model is Category and identifier == self.category.id
        return self.category

    def scalars(self, statement):
        model = statement.column_descriptions[0]['entity']
        return ScalarRows({Category: [self.category], CredentialFormat: self.formats,
                           Site: [], SiteUploadTemplate: []}[model])

    def execute(self, statement):
        assert statement.column_descriptions[0]['entity'] is CredentialFormat
        return [(fmt, self.category) for fmt in self.formats]


@pytest.mark.parametrize('family', ['AWS', 'Google', 'Azure', 'Anthropic', 'OpenAI', 'OpenRouter', 'OpenCode'])
def test_both_catalog_endpoints_publish_only_intended_defaults(family):
    session = CatalogSession(family)
    flat = catalog.formats(db=session, user=None)['items']
    nested = catalog.categories(db=session, user=None)['items'][0]['formats']
    assert flat == nested
    for row in flat:
        expected = EXPECTED_CLAUDE_MODELS if row['schema_config']['type'] in ('aws_claude', 'vertex_claude') else []
        assert row['default_models'] == expected
    assert all(fmt.default_models == ['historical-stored-default'] for fmt in session.formats)


@pytest.mark.parametrize(('family', 'variant'), [
    ('AWS', 'aws_claude'), ('AWS', 'bedrock'),
    ('Google', 'vertex_claude'), ('Google', 'vertex_gemini'), ('Google', 'ai_studio_gemini'),
    ('Azure', 'azure_claude'), ('Azure', 'azure_gpt'),
])
def test_upload_public_format_defaults_do_not_grant_receiving_models(family, variant):
    session = CatalogSession(family)
    public, targets, _, _ = _resolve_templates(session, family, variant=variant)
    assert public['formats']
    for row in public['formats']:
        expected = EXPECTED_CLAUDE_MODELS if variant in ('aws_claude', 'vertex_claude') else []
        assert row['default_models'] == expected
    assert public['models'] == public['configured_models'] == []
    assert not public['ready'] and not targets


def test_default_model_lists_are_independent_and_keep_exact_order():
    schema = {'type': 'aws_claude', 'remote_type': 14}
    result = format_default_models(schema)
    assert result == list(CLAUDE9) == EXPECTED_CLAUDE_MODELS
    result.reverse()
    result.append('custom-model')
    assert format_default_models(schema) == EXPECTED_CLAUDE_MODELS
    assert format_default_models({'type': 'vertex_claude', 'remote_type': 41}) == EXPECTED_CLAUDE_MODELS


def test_aws_claude_effective_models_remain_the_configured_intersection():
    session = CatalogSession('AWS')
    fmt = next(fmt for fmt in session.formats if fmt.schema_config['type'] == 'aws_claude')
    template = SimpleNamespace(models=['claude-opus-4-6', 'claude-sonnet-4-6'], channel_config={'status': 2})
    result = effective_template(template, {'models': ['claude-opus-4-6', 'claude-opus-5']},
                                category=session.category, fmt=fmt)
    assert result['models'] == ['claude-opus-4-6']
    assert result['channel_config']['status'] == 2
    assert template.models == ['claude-opus-4-6', 'claude-sonnet-4-6']
