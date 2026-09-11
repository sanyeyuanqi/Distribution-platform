"""Fixed credential contracts and original-record binding; no network access."""
import json
from types import SimpleNamespace

import pytest
from app.bootstrap import seed_catalog, seed_newapi_formats
from app.channel_service import parse_rows, supported_format
from app.models import Category, CredentialFormat, SiteUploadTemplate
from app.newapi_formats import credential_hint, get_format_specs, normalize_credential
from sqlalchemy import select


def fmt(kind, remote_type=None):
    spec = next(s for s in get_format_specs() if s['schema_config']['type'] == kind
                and (remote_type is None or s['remote_type'] == remote_type))
    return SimpleNamespace(**spec, enabled=True, category_id='category', id='format')


@pytest.mark.parametrize(('kind', 'value'), [
    ('aws_ak_sk', 'AKIA_TEST|private-test-key|us-east-1'),
    ('aws_api_key', 'private-api-token|eu-west-1'),
    ('baidu_pair', 'test-api|test-secret'),
    ('zhipu_pair', 'testid.testsecret'),
    ('xunfei_triple', 'test-app|test-secret|test-key'),
    ('tencent_triple', 'test-app|test-secret-id|test-secret-key'),
    ('kling_pair', 'test-access|test-secret'),
    ('jimeng_pair', 'test-access|test-secret'),
])
def test_fixed_plain_and_composite_formats(kind, value):
    assert normalize_credential(' ' + value + '\n', fmt(kind)) == value
    with pytest.raises(ValueError):
        normalize_credential('malformed', fmt(kind))


def test_json_records_canonical_dedup_and_per_record_metadata():
    first = {'project_id': 'project-a', 'private_key': 'SECRET\nMULTILINE', 'client_email': 'a@example.test'}
    same = dict(reversed(list(first.items())))
    second = {**first, 'project_id': 'project-b'}
    keys = json.dumps([first, same, second], indent=2)
    rows, errors = parse_rows(keys, 'one\none\nthree', 'socks5://u:private-password@proxy.test:1080', fmt=fmt('vertex_json'))
    assert not errors
    assert [r['status'] for r in rows] == ['valid', 'duplicate', 'valid']
    assert [r['remark'] for r in rows] == ['one', 'one', 'three']
    assert all('SECRET' not in r['key_hint'] and 'private-password' not in r['proxy_hint'] for r in rows)
    assert rows[0]['fingerprint'] == rows[1]['fingerprint']
    assert json.loads(rows[0]['key']) == first


def test_json_single_pretty_and_jsonl_blank_row_binding():
    obj = {'access_token': 'private-token', 'account_id': 'test-account', 'refresh_token': 'private-refresh'}
    pretty = json.dumps(obj, indent=2)
    rows, errors = parse_rows(pretty, fmt=fmt('codex_json'), upload_mode='single')
    assert not errors and len(rows) == 1 and rows[0]['status'] == 'valid'
    raw = json.dumps(obj)
    rows, errors = parse_rows(raw + '\n\n' + raw, 'first\nblank\nthird', fmt=fmt('codex_json'))
    assert not errors and [r['remark'] for r in rows] == ['first', 'blank', 'third']
    assert rows[1]['status'] == 'invalid' and rows[2]['status'] == 'conflict'


@pytest.mark.parametrize('value', [
    '{"access_token":"a","access_token":"b","account_id":"c"}',
    '{"access_token":"a","account_id":"b","other":NaN}',
    '{"access_token":null,"account_id":"b"}',
    '[]', '"private-secret"', '{"private_key":"private-secret"}',
])
def test_json_rejects_invalid_shape_without_echo(value):
    with pytest.raises(ValueError) as result:
        normalize_credential(value, fmt('codex_json'))
    assert 'private-secret' not in str(result.value)


def test_single_mode_and_batch_limits_enforced_server_side():
    rows, errors = parse_rows('key-one\nkey-two', fmt=fmt('api_key', 1), upload_mode='single')
    assert len(rows) == 2 and any('单密钥' in e for e in errors)
    _, errors = parse_rows('key-one\nkey-two', limit=1)
    assert any('最多' in e for e in errors)


def test_unknown_or_modified_schema_is_not_executable():
    category = SimpleNamespace(id='category', name='OpenAI', family='OpenAI', active=True)
    candidate = fmt('api_key', 1)
    assert supported_format(category, candidate)
    candidate.schema_config = {'type': 'api_key', 'remote_type': 57}
    assert not supported_format(category, candidate)
    with pytest.raises(ValueError):
        normalize_credential('test-key', candidate)


def test_catalog_addition_is_idempotent_and_preserves_template_format_references(db):
    seed_catalog(db)
    aws = db.scalar(select(Category).where(Category.family == 'AWS'))
    pending = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == aws.id))
    original_id = pending.id
    seed_newapi_formats(db)
    before = list(db.scalars(select(CredentialFormat)))
    assert pending.id == original_id and pending.schema_config['remote_type'] == 33 and pending.enabled
    seed_newapi_formats(db)
    assert len(list(db.scalars(select(CredentialFormat)))) == len(before)
    assert {s['remote_type'] for s in get_format_specs()} >= {1, 3, 14, 20, 24, 33, 41, 57, 59, 60}
    assert len(list(db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == aws.id)))) == 4
    assert not list(db.scalars(select(SiteUploadTemplate)))


def test_json_hint_never_contains_credential_fields():
    value = json.dumps({'access_token': 'start-private-end', 'account_id': 'private-account'})
    hint = credential_hint(value, fmt('codex_json'))
    assert hint.startswith('JSON ••••') and 'private' not in hint and 'start' not in hint
