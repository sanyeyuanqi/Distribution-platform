"""Invocation equivalence ignores seller metadata but preserves protocol differences."""
from copy import deepcopy

import pytest
from app.channel_local_tests import invocation_signature


def source(remote_type, config, *, wire=None):
    return {'snapshot': {'format_schema': {'remote_type': remote_type}, 'test_wire_type': wire,
                         'test_inputs': {'channel_config': config}}}


@pytest.mark.parametrize('remote_type', [1, 3, 14, 20, 24, 33, 41])
def test_unused_seller_settings_and_other_models_do_not_make_false_conflicts(remote_type):
    first = source(remote_type, {'model_mapping': {'chosen': 'target'}, 'base_url': 'https://api.openai.com',
                                 'other': 'global', 'setting': {}})
    second = deepcopy(first)
    config = second['snapshot']['test_inputs']['channel_config']
    config.update(status=1, rpm_enabled=True, rpm_limit=100, account_info={'tpm': 900000}, group='unrelated')
    config['model_mapping']['unselected'] = 'different-target'
    config['setting'] = '{"force_format": true}'
    assert invocation_signature(first, 'chosen') == invocation_signature(second, 'chosen')


@pytest.mark.parametrize('base', ['', 'https://api.openai.com', 'https://api.openai.com/', 'https://api.openai.com/v1/'])
def test_openai_official_base_spellings_are_equivalent(base):
    assert invocation_signature(source(1, {'base_url': base}), 'model') == invocation_signature(source(1, {}), 'model')


def test_bedrock_explicit_mapping_changes_alias_resolution_even_when_text_matches():
    plain = source(33, {})
    explicit = source(33, {'model_mapping': {'claude-opus-4-6': 'claude-opus-4-6'}})
    assert invocation_signature(plain, 'claude-opus-4-6') != invocation_signature(explicit, 'claude-opus-4-6')
    assert invocation_signature(source(33, {'base_url': 'unused', 'other': 'unused'}), 'model') == invocation_signature(plain, 'model')


def test_vertex_region_uses_selected_model_only_and_express_location_is_not_an_endpoint():
    first = source(41, {'other': '{"default":"global","chosen":"us-east1","other":"us-west1"}'}, wire='vertex_json')
    second = source(41, {'other': {'chosen': 'us-east1', 'other': 'europe-west1'}}, wire='vertex_json')
    assert invocation_signature(first, 'chosen') == invocation_signature(second, 'chosen')
    second['snapshot']['test_inputs']['channel_config']['other']['chosen'] = 'us-east5'
    assert invocation_signature(first, 'chosen') != invocation_signature(second, 'chosen')
    first['snapshot']['test_wire_type'] = second['snapshot']['test_wire_type'] = 'vertex_api_key'
    assert invocation_signature(first, 'chosen') == invocation_signature(second, 'chosen')


def test_setting_json_proxy_is_compared_without_exposing_or_raising():
    parsed = source(1, {'setting': {'proxy': 'http://proxy.invalid:8080'}})
    encoded = source(1, {'setting': '{"proxy":"http://proxy.invalid:8080"}'})
    assert invocation_signature(parsed, 'model') == invocation_signature(encoded, 'model')
    assert invocation_signature(encoded, 'model') != invocation_signature(source(1, {}), 'model')
    for value in ('null', '[]', 'not-json', True):
        assert invocation_signature(source(1, {'setting': value}), 'model') == invocation_signature(source(1, {}), 'model')
