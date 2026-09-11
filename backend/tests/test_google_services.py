"""Pure contract tests: no provider requests or application writes."""
from copy import deepcopy

import pytest
from app.google_services import (
    CLAUDE9,
    GOOGLE_VARIANTS,
    google_model_mapping,
    google_service_variant,
    service_model_issues,
)


def test_three_fixed_google_services_and_exact_requested_claude_scope():
    assert GOOGLE_VARIANTS == ('ai_studio_gemini', 'vertex_gemini', 'vertex_claude')
    assert len(CLAUDE9) == len(set(CLAUDE9)) == 9
    assert set(CLAUDE9) == {
        'claude-haiku-4-5-20251001', 'claude-opus-4-6', 'claude-opus-4-7',
        'claude-opus-4-8', 'claude-sonnet-5', 'claude-fable-5', 'claude-opus-5',
        'claude-sonnet-4-6', 'claude-fable-5-1',
    }


@pytest.mark.parametrize(('schema', 'models', 'expected'), [
    ({'type': 'api_key', 'remote_type': 24}, [], 'ai_studio_gemini'),
    ({'type': 'vertex_gemini', 'remote_type': 41}, None, 'vertex_gemini'),
    ({'type': 'vertex_claude', 'remote_type': 41}, None, 'vertex_claude'),
    ({'type': 'vertex_json', 'remote_type': 41}, ['gemini-3.1-pro'], 'vertex_gemini'),
    ({'type': 'vertex_api_key', 'remote_type': 41}, ['gemini-3.1-pro'], 'vertex_gemini'),
    ({'type': 'vertex_json', 'remote_type': 41}, list(CLAUDE9), 'vertex_claude'),
    ({'type': 'vertex_api_key', 'remote_type': 41}, list(CLAUDE9), 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, ['gemini-3.1-pro', 'claude-opus-4-6'], 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, ['imagen-4.0-generate-001'], 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, [], 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, None, 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, 'claude-opus-4-6', 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, [''], 'vertex_legacy'),
    ({'type': 'vertex_json', 'remote_type': 41}, [None], 'vertex_legacy'),
    ({'type': 'other', 'remote_type': 41}, ['gemini-3.1-pro'], 'vertex_legacy'),
    ({'type': 'api_key', 'remote_type': 14}, list(CLAUDE9), ''),
    (None, list(CLAUDE9), ''),
])
def test_service_classification_does_not_guess_legacy_protocol(schema, models, expected):
    before = deepcopy((schema, models))
    assert google_service_variant(schema, models) == expected
    assert (schema, models) == before


def test_vertex_mapping_only_converts_dated_haiku_inside_effective_scope():
    models = list(CLAUDE9)
    assert google_model_mapping('vertex_claude', models) == {
        'claude-haiku-4-5-20251001': 'claude-haiku-4-5@20251001',
    }
    assert google_model_mapping('vertex_claude', ['claude-opus-4-6']) == {}
    assert google_model_mapping('vertex_claude', []) == {}
    assert models == list(CLAUDE9)


@pytest.mark.parametrize('kind', ['vertex_gemini', 'ai_studio_gemini', 'vertex_json',
                                 'vertex_api_key', 'vertex_legacy', 'Anthropic', None])
def test_mapping_does_not_change_other_services_or_historical_formats(kind):
    assert google_model_mapping(kind, list(CLAUDE9)) == {}


def test_mapping_never_invents_unknown_models_or_forwards_invalid_entries():
    assert google_model_mapping('vertex_claude', ['claude-opus-99', 'claude-haiku-4-5',
        'claude-haiku-4-5@20251001', None, {}]) == {}
    assert google_model_mapping('vertex_claude', None) == {}


@pytest.mark.parametrize('model', CLAUDE9)
def test_new_vertex_claude_accepts_each_requested_model(model):
    assert service_model_issues({'type': 'vertex_claude', 'remote_type': 41}, [model]) == []


@pytest.mark.parametrize('models', [['gemini-3.1-pro'], ['claude-opus-99'],
    ['claude-haiku-4-5@20251001'], ['claude-opus-4-6', 'gemini-3.1-pro'], [None], [{}]])
def test_new_vertex_claude_rejects_cross_service_unknown_or_wire_only_names(models):
    assert service_model_issues({'type': 'vertex_claude', 'remote_type': 41}, models)


def test_new_vertex_gemini_rejects_claude_but_preserves_existing_google_models():
    schema = {'type': 'vertex_gemini', 'remote_type': 41}
    assert service_model_issues(schema, ['gemini-3.1-pro', 'imagen-4.0-generate-001']) == []
    assert service_model_issues(schema, ['gemini-3.1-pro', 'claude-opus-4-6'])
    assert service_model_issues(schema, list(CLAUDE9))


@pytest.mark.parametrize('schema', [
    {'type': 'api_key', 'remote_type': 24},
    {'type': 'vertex_gemini', 'remote_type': 41},
])
@pytest.mark.parametrize('model', [
    'claude-opus-4-6', 'anthropic/claude-opus-4.6', 'anthropic.claude-opus-4-6-v1',
    'publishers/anthropic/models/claude-opus-4-6',
    'projects/example/locations/global/publishers/anthropic/models/claude-opus-4-6',
    'publishers/anthropic', ' ANTHROPIC/CLAUDE-OPUS-4.6 ', 'claude', 'claude_custom',
])
def test_gemini_services_reject_explicit_claude_provider_aliases(schema, model):
    assert service_model_issues(schema, ['gemini-3.1-pro', model])


@pytest.mark.parametrize('schema', [
    {'type': 'api_key', 'remote_type': 24},
    {'type': 'vertex_gemini', 'remote_type': 41},
])
def test_gemini_services_preserve_non_claude_google_names(schema):
    assert service_model_issues(schema, ['gemini-3.1-pro', 'imagen-4.0-generate-001',
        'gemini-embedding-001', 'veo-3.1-generate-001']) == []


@pytest.mark.parametrize('kind', ['vertex_json', 'vertex_api_key', 'api_key'])
def test_historical_models_are_not_revalidated_as_new_service(kind):
    schema = {'type': kind, 'remote_type': 41}
    models = ['unknown-legacy-name', 'claude-opus-4-6', 'gemini-3.1-pro']
    before = deepcopy((schema, models))
    assert service_model_issues(schema, models) == []
    assert (schema, models) == before


def test_empty_models_remain_draft_validated_elsewhere():
    assert service_model_issues({'type': 'vertex_gemini', 'remote_type': 41}, []) == []
    assert service_model_issues({'type': 'vertex_claude', 'remote_type': 41}, []) == []
