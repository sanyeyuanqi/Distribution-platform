"""Pure contracts for provider wire names; no database or network fixtures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.catalog_policy import catalog_model_mapping

UPLOAD_CLAUDE_NAMES = [
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


def category(family, name=None):
    return SimpleNamespace(family=family, name=name or family)


def test_openrouter_uses_exact_provider_ids_for_the_nine_upload_models():
    assert catalog_model_mapping(category('OpenRouter'), UPLOAD_CLAUDE_NAMES, {}) == {
        'claude-haiku-4-5-20251001': 'anthropic/claude-haiku-4.5',
        'claude-opus-4-6': 'anthropic/claude-opus-4.6',
        'claude-opus-4-7': 'anthropic/claude-opus-4.7',
        'claude-opus-4-8': 'anthropic/claude-opus-4.8',
        'claude-sonnet-5': 'anthropic/claude-sonnet-5',
        'claude-fable-5': 'anthropic/claude-fable-5',
        'claude-opus-5': 'anthropic/claude-opus-5',
        'claude-sonnet-4-6': 'anthropic/claude-sonnet-4.6',
        'claude-fable-5-1': 'anthropic/claude-fable-5.1',
    }


def test_opencode_only_rewrites_the_dated_haiku_id():
    assert catalog_model_mapping(category('OpenCode'), UPLOAD_CLAUDE_NAMES, None) == {
        'claude-haiku-4-5-20251001': 'claude-haiku-4-5',
    }


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode'])
def test_automatic_mapping_never_adds_models_outside_the_effective_scope(family):
    assert catalog_model_mapping(category(family), [], {}) == {}
    assert catalog_model_mapping(category(family), ['unrelated-model'], {}) == {}
    selected = ['claude-fable-5-1', 'claude-fable-5-1']
    expected = {'claude-fable-5-1': 'anthropic/claude-fable-5.1'} if family == 'OpenRouter' else {}
    assert catalog_model_mapping(category(family), selected, {}) == expected


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode'])
def test_explicit_historical_mapping_wins_and_inputs_are_not_mutated(family):
    models = ['claude-haiku-4-5-20251001', 'claude-opus-5']
    explicit = {
        'claude-haiku-4-5-20251001': 'account-specific-haiku',
        'legacy-unselected-model': 'account-specific-legacy',
    }
    original_models, original_explicit = deepcopy(models), deepcopy(explicit)

    result = catalog_model_mapping(category(family), models, explicit)

    assert result['claude-haiku-4-5-20251001'] == 'account-specific-haiku'
    assert result['legacy-unselected-model'] == 'account-specific-legacy'
    assert result.get('claude-opus-5') == ('anthropic/claude-opus-5' if family == 'OpenRouter' else None)
    assert models == original_models
    assert explicit == original_explicit
    assert result is not explicit


@pytest.mark.parametrize('family', ['AWS', 'Anthropic', 'OpenAI', 'Azure', 'Google', 'Unknown'])
def test_other_families_preserve_explicit_mapping_without_generating_aliases(family):
    explicit = {'custom-name': 'custom-upstream-name'}
    assert catalog_model_mapping(category(family), UPLOAD_CLAUDE_NAMES, explicit) == explicit
    assert catalog_model_mapping(category(family), UPLOAD_CLAUDE_NAMES, None) == {}


def test_family_selects_protocol_instead_of_the_display_name():
    assert catalog_model_mapping(category('Anthropic', name='OpenRouter'), ['claude-opus-5'], {}) == {}
    assert catalog_model_mapping(category('OpenRouter', name='Custom display label'), ['claude-opus-5'], {}) == {
        'claude-opus-5': 'anthropic/claude-opus-5',
    }
    assert catalog_model_mapping(None, UPLOAD_CLAUDE_NAMES, {'custom': 'target'}) == {'custom': 'target'}


@pytest.mark.parametrize('family', ['OpenRouter', 'OpenCode'])
def test_unknown_aliases_and_already_canonical_ids_are_not_guessed(family):
    model_names = [
        'claude-opus-6', 'claude-opus-5-thinking', 'claude-opus-4-8-max',
        'claude-opus-4.8', 'claude-haiku-4-5-20261001',
        'anthropic/claude-opus-5', 'anthropic.claude-opus-5',
        'claude-opus-5 ', 'CLAUDE-OPUS-5', 'gemini-3.8-flash',
    ]
    assert catalog_model_mapping(category(family), model_names, {}) == {}
