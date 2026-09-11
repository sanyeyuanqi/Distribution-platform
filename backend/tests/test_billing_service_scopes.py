"""Pure timeline and frozen-service contracts; no database fixtures."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.billing_services import (
    backfill_applies,
    frozen_service_variant,
    scoped_discount,
    scoped_transition,
    service_definitions,
    service_projection,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)


def version(identifier, variant, percent, days=0, inherit=False):
    return SimpleNamespace(id=identifier, service_variant=variant, percent=Decimal(percent),
                           effective_at=NOW + timedelta(days=days), inherits_category=inherit)


def test_scoped_rate_precedes_newer_category_default_and_zero_never_inherits():
    history = [version('base-old', None, 80, -4), version('bedrock', 'bedrock', 25, -3),
               version('claude-zero', 'aws_claude', 0, -2), version('base-new', None, 90, -1)]
    assert scoped_discount(history, NOW, 'bedrock').percent == 25
    assert scoped_discount(history, NOW, 'aws_claude').percent == 0
    assert scoped_discount(history, NOW).percent == 90


def test_future_override_and_append_only_reset_follow_occurrence_time():
    history = [version('base', None, 80, -2), version('bedrock', 'bedrock', 30, -1),
               version('reset', 'bedrock', 0, 1, inherit=True), version('next-base', None, 85, 2)]
    assert scoped_discount(history, NOW, 'bedrock').id == 'bedrock'
    assert scoped_discount(history, NOW + timedelta(days=1), 'bedrock').id == 'base'
    assert scoped_discount(history, NOW + timedelta(days=2), 'bedrock').id == 'next-base'
    assert scoped_discount(history, NOW - timedelta(days=3), 'bedrock') is None


def test_interval_ignores_other_services_and_shadowed_category_changes():
    history = [version('base', None, 80, -2), version('bedrock', 'bedrock', 30, -1),
               version('claude', 'aws_claude', 15, 1), version('next-base', None, 85, 2)]
    assert not scoped_transition(history, NOW, NOW + timedelta(days=3), 'bedrock')
    assert scoped_transition(history, NOW, NOW + timedelta(days=3), 'aws_claude')
    assert scoped_transition(history, NOW, NOW + timedelta(days=3), None)


def test_backfill_cannot_cross_services_or_erase_an_explicit_zero_scope():
    fine = version('fine', 'bedrock', 20)
    default = version('default', None, 80)
    zero = version('zero', 'bedrock', 0)
    assert backfill_applies(fine, 'bedrock', zero)
    assert not backfill_applies(fine, 'aws_claude', None)
    assert not backfill_applies(fine, None, None)
    assert not backfill_applies(default, 'bedrock', zero)
    assert backfill_applies(default, 'bedrock', None)
    assert not backfill_applies(version('reset', 'bedrock', 0, inherit=True), 'bedrock', None)


def test_single_service_empty_scope_is_distinct_from_default_and_unknown():
    history = [version('base', None, 80, -2), version('official', '', 20, -1)]
    assert scoped_discount(history, NOW, '').id == 'official'
    assert scoped_discount(history, NOW, None).id == 'base'


@pytest.mark.parametrize(('family', 'kind', 'remote_type', 'expected'), [
    ('AWS', 'aws_bedrock', 33, 'bedrock'), ('AWS', 'aws_ak_sk', 33, 'bedrock'),
    ('AWS', 'aws_claude', 14, 'aws_claude'), ('Azure', 'azure_gpt', 3, 'azure_gpt'),
    ('Azure', 'azure_claude', 14, 'azure_claude'), ('Google', 'api_key', 24, 'ai_studio_gemini'),
    ('Google', 'vertex_gemini', 41, 'vertex_gemini'), ('Google', 'vertex_claude', 41, 'vertex_claude'),
    ('Google', 'vertex_json', 41, None), ('Google', 'vertex_api_key', 41, None),
    ('AWS', 'api_key', 14, None), ('OpenAI', 'api_key', 1, ''),
])
def test_fact_service_uses_explicit_format_not_current_model_guesses(family, kind, remote_type, expected):
    category = SimpleNamespace(family=family)
    fmt = SimpleNamespace(schema_config={'type': kind, 'remote_type': remote_type})
    assert frozen_service_variant(category, fmt) == expected


def test_service_definitions_reuse_the_eleven_template_service_names_in_order():
    families = ['Google', 'OpenCode', 'Azure', 'Anthropic', 'AWS', 'OpenRouter', 'OpenAI']
    categories = [SimpleNamespace(id=family, family=family) for family in families]
    definitions = service_definitions(categories)
    assert len(definitions) == 11
    assert [definition['label'] for definition in definitions] == [
        'AWS Bedrock', 'AWS Claude 代理', 'Anthropic 官方', 'OpenAI', 'Azure OpenAI', 'Azure Claude',
        'Google AI Studio', 'Vertex AI Gemini', 'Vertex AI Claude', 'OpenRouter', 'OpenCode']


def test_projection_explains_future_reset_and_hides_irrelevant_schedule():
    history = [version('base', None, 80, -2), version('bedrock', 'bedrock', 20, -1),
               version('other', 'aws_claude', 50, 1), version('base-shadowed', None, 90, 2),
               version('reset', 'bedrock', 0, 3, inherit=True)]
    result = service_projection({'service_variant': 'bedrock'}, history, NOW, vars)
    assert result['current']['id'] == 'bedrock' and not result['inherited']
    assert len(result['future']) == 1
    assert result['future'][0]['effective_at'] == NOW + timedelta(days=3)
    assert result['future'][0]['inherited']
    assert result['future'][0]['current']['id'] == 'base-shadowed'
