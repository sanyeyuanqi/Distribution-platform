"""Logical credential records from consecutive pasted service-account exports."""
import json
from types import SimpleNamespace

import pytest
from app.newapi_formats import credential_hint, credential_records, normalize_credential


def fmt(kind):
    return SimpleNamespace(schema_config={'type': kind, 'remote_type': 41})


def account(name):
    return {'project_id': name, 'client_email': name + '@example.invalid',
            'private_key': 'fixture\ntext with escaped "quote" and {braces}\nend'}


@pytest.mark.parametrize('kind', ['vertex_gemini', 'vertex_claude', 'vertex_json'])
@pytest.mark.parametrize('separator', ['', '\n', '\n\n', '\r\n\r\n'])
def test_consecutive_pretty_objects_keep_private_key_strings_intact(kind, separator):
    first, second = account('first'), account('second')
    text = json.dumps(first, indent=2) + separator + json.dumps(second, indent=2)
    records, positions = credential_records(text, fmt(kind))
    assert len(records) == len(positions) == 2
    assert positions[0] == 1
    assert [json.loads(normalize_credential(value, fmt(kind))) for value in records] == [first, second]


@pytest.mark.parametrize('kind', ['vertex_gemini', 'vertex_claude'])
def test_mixed_pretty_json_and_keys_bind_to_logical_record_starts(kind):
    pretty = json.dumps(account('fixture'), indent=2)
    text = 'key-first\n' + pretty + '\n\nkey-last'
    records, positions = credential_records(text, fmt(kind))
    assert len(records) == 3
    assert positions == [1, 2, len(pretty.splitlines()) + 3]
    assert normalize_credential(records[0], fmt(kind)) == 'key-first'
    assert json.loads(normalize_credential(records[1], fmt(kind))) == account('fixture')
    assert normalize_credential(records[2], fmt(kind)) == 'key-last'


@pytest.mark.parametrize('kind', ['vertex_gemini', 'vertex_claude'])
def test_arrays_and_individual_records_can_be_consecutively_pasted(kind):
    first, second = account('one'), account('two')
    text = json.dumps([first, 'key-one'], indent=2) + '\n' + json.dumps(second, indent=2) + '\nkey-two'
    records, _ = credential_records(text, fmt(kind))
    assert [normalize_credential(value, fmt(kind)) for value in records] == [
        normalize_credential(json.dumps(first), fmt(kind)), 'key-one',
        normalize_credential(json.dumps(second), fmt(kind)), 'key-two',
    ]


@pytest.mark.parametrize('kind', ['vertex_gemini', 'vertex_claude'])
def test_equivalent_json_records_normalize_to_same_credential(kind):
    obj = account('same')
    text = json.dumps(obj, indent=2) + '\n' + json.dumps(dict(reversed(list(obj.items()))))
    records, _ = credential_records(text, fmt(kind))
    assert normalize_credential(records[0], fmt(kind)) == normalize_credential(records[1], fmt(kind))


@pytest.mark.parametrize('fragment', ['{"project_id":', '{"project_id":"a","project_id":"b"}',
                                     '{"private_key":NaN}', '{"project_id":"unclosed'])
def test_malformed_json_keeps_original_rows_for_validation_instead_of_disappearing(fragment):
    text = json.dumps(account('valid'), indent=2) + '\n' + fragment
    records, positions = credential_records(text, fmt('vertex_gemini'))
    assert records == text.splitlines()
    assert positions == list(range(1, len(records) + 1))
    with pytest.raises(ValueError):
        normalize_credential(records[-1], fmt('vertex_gemini'))


def test_plain_keys_keep_original_blank_line_validation():
    assert credential_records('key-one\n\nkey-two', fmt('vertex_claude')) == (['key-one', '', 'key-two'], [1, 2, 3])


def test_api_key_cannot_be_glued_to_json_on_same_line():
    text = json.dumps(account('one')) + 'key-two'
    records, _ = credential_records(text, fmt('vertex_claude'))
    assert records == [text]
    with pytest.raises(ValueError):
        normalize_credential(records[0], fmt('vertex_claude'))


def test_claude_api_key_and_json_hints_follow_actual_authentication():
    definition = fmt('vertex_claude')
    key = normalize_credential('vertex-fixture-key', definition)
    assert not credential_hint(key, definition).startswith('JSON ')
    normalized = normalize_credential(json.dumps(account('one')), definition)
    assert credential_hint(normalized, definition).startswith('JSON ')
