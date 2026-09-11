"""Optional-region input reaches the ledger only as complete NewAPI credentials."""
# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from app import worker
from app.channel_service import parse_rows
from app.models_channels import Channel, KeyVersion, Task, TaskItem
from app.newapi_formats import get_format_specs, normalize_credential
from app.security import decrypt, fingerprint
from sqlalchemy import select
from test_aws_distribution import attach
from test_aws_template_variants import (  # noqa: F401
    aws,
    create_template,
    no_external_calls,
    preview,
    submit,
)

ACCESS_ID = 'AKIA' + 'A' * 16
NEXT_ACCESS_ID = 'AKIA' + 'B' * 16
SESSION_ACCESS_ID = 'ASIA' + 'C' * 16
IAM = ACCESS_ID + '|private-secret'
CANONICAL_IAM = IAM + '|us-east-1'
API = 'bedrock-api-private-key'
CANONICAL_API = API + '|us-east-1'


def format_for(kind):
    return SimpleNamespace(**next(spec for spec in get_format_specs()
                                 if spec['family'] == 'AWS' and spec['schema_config']['type'] == kind))


@pytest.mark.parametrize(('raw', 'expected'), [
    (IAM, CANONICAL_IAM),
    (API, CANONICAL_API),
    (IAM + '|eu-west-1', IAM + '|eu-west-1'),
    (API + '|ap-southeast-1', API + '|ap-southeast-1'),
])
def test_merged_normalization_adds_only_missing_region(raw, expected):
    fmt = format_for('aws_bedrock')
    assert normalize_credential(raw, fmt) == expected
    assert normalize_credential(expected, fmt) == expected


def test_iam_session_pair_needs_a_bedrock_token_instead_of_only_a_region():
    with pytest.raises(ValueError, match='会话令牌'):
        normalize_credential(SESSION_ACCESS_ID + '|temporary-secret', format_for('aws_bedrock'))


@pytest.mark.parametrize(('kind', 'missing', 'complete'), [
    ('aws_ak_sk', IAM, CANONICAL_IAM),
    ('aws_api_key', API, CANONICAL_API),
])
def test_legacy_formats_keep_explicit_region_contract(kind, missing, complete):
    fmt = format_for(kind)
    with pytest.raises(ValueError):
        normalize_credential(missing, fmt)
    assert normalize_credential(complete, fmt) == complete


def test_merged_dedup_uses_canonical_key_and_keeps_original_line_metadata():
    proxy = 'socks5://fixture-user:fixture-password@proxy.example:1080'
    rows, errors = parse_rows('\n' + IAM + '\n' + API + '\n' + CANONICAL_IAM + '\n',
        remarks='iam note\napi note\niam note', proxies=proxy, fmt=format_for('aws_bedrock'))
    assert errors == []
    assert [row['line'] for row in rows] == [2, 3, 4]
    assert [row['status'] for row in rows] == ['valid', 'valid', 'duplicate']
    assert [row['remark'] for row in rows] == ['iam note', 'api note', 'iam note']
    assert all(row['proxy'] == proxy for row in rows)
    assert rows[0]['key'] == rows[2]['key'] == CANONICAL_IAM
    assert rows[0]['fingerprint'] == rows[2]['fingerprint'] == fingerprint(CANONICAL_IAM)
    assert '第 2 行' in rows[2]['message']


def test_optional_region_does_not_drop_interior_empty_rows_or_shift_metadata():
    rows, errors = parse_rows('\n' + IAM + '\n\n' + API + '\n',
        remarks='iam note\nempty note\napi note',
        proxies='http://iam.proxy.example:81\n\nhttp://api.proxy.example:82', fmt=format_for('aws_bedrock'))
    assert errors == []
    assert [row['line'] for row in rows] == [2, 3, 4]
    assert [row['status'] for row in rows] == ['valid', 'invalid', 'valid']
    assert rows[0]['remark'] == 'iam note' and rows[0]['proxy'] == 'http://iam.proxy.example:81'
    assert rows[2]['key'] == CANONICAL_API
    assert rows[2]['remark'] == 'api note' and rows[2]['proxy'] == 'http://api.proxy.example:82'


def test_implicit_and_explicit_duplicate_with_different_metadata_is_conflict():
    rows, errors = parse_rows(API + '\n' + CANONICAL_API, remarks='first note\nsecond note',
        fmt=format_for('aws_bedrock'))
    assert errors == []
    assert [row['status'] for row in rows] == ['conflict', 'conflict']
    assert rows[0]['fingerprint'] == rows[1]['fingerprint']


def test_preview_and_submit_freeze_complete_keys_and_per_record_wire_types(db, login, aws):
    create_template(login('root'), aws)
    client = login('user')
    raw = IAM + '\n' + API + '\n' + CANONICAL_IAM + '\n' + CANONICAL_API
    response = preview(client, aws, credentials=raw)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['can_submit'] and plan['valid_count'] == 2 and plan['duplicate_count'] == 2
    assert [row['line'] for row in plan['rows']] == [1, 2, 3, 4]
    assert IAM not in response.text and API not in response.text
    result = submit(client, aws, credentials=raw, configuration_revision=plan['configuration_revision'])
    assert result['total'] == 2
    actual = {}
    for row in result['items']:
        item = db.get(TaskItem, row['id'])
        channel = db.get(Channel, item.channel_id)
        key = decrypt(channel.key_encrypted)
        actual[key] = item.snapshot['wire_format_schema']
        assert item.snapshot['format_schema'] == {'type': 'aws_bedrock', 'remote_type': 33}
        assert channel.fingerprint == fingerprint(key)
        history = db.scalar(select(KeyVersion).where(KeyVersion.channel_id == channel.id, KeyVersion.version == 1))
        assert decrypt(history.key_encrypted) == key
    assert actual == {
        CANONICAL_IAM: {'type': 'aws_ak_sk', 'remote_type': 33},
        CANONICAL_API: {'type': 'aws_api_key', 'remote_type': 33},
    }
    # Re-pasting the same credentials with explicit regions must not create work.
    duplicate = preview(client, aws, credentials=CANONICAL_IAM + '\n' + CANONICAL_API).json()
    assert not duplicate['can_submit'] and duplicate['valid_count'] == 0
    assert all(row['status'] == 'already_distributed' for row in duplicate['rows'])


@pytest.mark.parametrize(('initial', 'replacement', 'other_mode', 'expected_kind'), [
    (IAM, NEXT_ACCESS_ID + '|replacement-secret', 'replacement-api-key', 'aws_ak_sk'),
    (API, 'replacement-api-key', NEXT_ACCESS_ID + '|replacement-secret', 'aws_api_key'),
])
def test_rotation_normalizes_region_and_rejects_auth_mode_change(db, login, aws, monkeypatch,
                                                               initial, replacement, other_mode, expected_kind):
    create_template(login('root'), aws)
    client = login('user')
    result = submit(client, aws, credentials=initial)
    attach(monkeypatch)
    item = db.get(TaskItem, result['items'][0]['id'])
    worker.execute_item(db, item)
    task = db.get(Task, result['id'])
    task.status, item.status = 'succeeded', 'succeeded'
    db.commit()
    channel = db.get(Channel, item.channel_id)
    original_key = decrypt(channel.key_encrypted)
    same = client.post(f'/api/channels/{channel.id}/rotate', json={'key': initial})
    assert same.status_code == 200 and same.json()['task_id'] is None
    rejected = client.post(f'/api/channels/{channel.id}/rotate', json={'key': other_mode})
    assert rejected.status_code == 422 and '类型' in rejected.json()['detail']
    db.refresh(channel)
    assert channel.key_version == 1 and decrypt(channel.key_encrypted) == original_key
    accepted = client.post(f'/api/channels/{channel.id}/rotate', json={'key': replacement})
    assert accepted.status_code == 200, accepted.text
    db.refresh(channel)
    assert channel.key_version == 2 and decrypt(channel.key_encrypted) == replacement + '|us-east-1'
    rotation = db.scalar(select(TaskItem).where(TaskItem.task_id == accepted.json()['task_id']))
    assert rotation is not None and rotation.snapshot['wire_format_schema'] == {'type': expected_kind, 'remote_type': 33}
