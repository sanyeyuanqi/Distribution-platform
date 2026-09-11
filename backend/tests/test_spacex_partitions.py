"""Pure partition contracts; no database fixtures or remote requests."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.credential_containers import partition_entries
from app.newapi_formats import get_format_specs
from app.security import fingerprint

FORMATS = [spec for spec in get_format_specs() if spec['remote_type'] in (1, 3, 14, 20, 24, 33, 41)]


def entries_for(kind):
    entries = []
    for index in range(2):
        key = f'fixture-key-{index}'
        if kind in ('vertex_json', 'vertex_gemini', 'vertex_claude'):
            key = json.dumps({'project_id': 'fixture-project', 'client_email': 'fixture@example.invalid',
                              'private_key': f'fixture-private-{index}'})
        elif kind in ('aws_ak_sk', 'aws_bedrock'):
            key = f'AKIAIOSFODNN7EXAMPLE|fixture-secret-{index}|us-east-1'
        elif kind == 'aws_api_key':
            key += '|us-east-1'
        elif kind in ('azure_gpt', 'azure_claude'):
            key = f'fixture-resource|{key}|2025-04-01-preview'
        entries.append({'key': key, 'remark': 'same remark', 'proxy': ''})
    return entries


@pytest.mark.parametrize('spec', FORMATS, ids=lambda spec: spec['code'])
def test_spacex_splits_every_format_and_keeps_partition_identity_after_auth_refresh(spec):
    fmt = SimpleNamespace(**spec)
    entries = entries_for(spec['schema_config']['type'])
    original = deepcopy(entries)
    site = SimpleNamespace(adapter='spacex-hub-v1', capabilities={'multi_key': 'unsupported', 'can_write': True},
                           seller_user_id='11')
    parts = partition_entries(entries, fmt, site=site)
    assert len(parts) == 2
    assert [part['key_count'] for part in parts] == [1, 1]
    assert [part['indices'] for part in parts] == [[0], [1]]
    assert [part['entries'][0] for part in parts] == entries
    assert len({part['partition_key'] for part in parts}) == 2
    assert entries == original

    # Reverification clears capabilities; a changed system-token account must not move
    # the same immutable local batch into different remote partitions.
    site.capabilities = {}
    site.seller_user_id = '12'
    assert partition_entries(entries, fmt, site=site) == parts
    site.capabilities = {'multi_key': 'supported', 'can_write': False}
    assert partition_entries(entries, fmt, site=site) == parts
    assert partition_entries(entries[:1], fmt, multiple=False, site=site)[0]['partition_key'] == ''


@pytest.mark.parametrize('adapter', ['silicon-v1', 'tcp-red-v1', 'new-api-v1'])
@pytest.mark.parametrize('kind', ['api_key', 'vertex_json', 'vertex_api_key'])
def test_existing_adapter_partition_keys_and_grouping_remain_unchanged(adapter, kind):
    spec = next(spec for spec in get_format_specs() if spec['schema_config']['type'] == kind)
    fmt = SimpleNamespace(**spec)
    site = SimpleNamespace(adapter=adapter, base_url='https://fixture.invalid')
    entries = entries_for(kind)
    parts = partition_entries(entries, fmt, site=site)
    split = kind == 'vertex_api_key' and adapter != 'new-api-v1'
    assert [part['key_count'] for part in parts] == ([1, 1] if split else [2])
    for index, part in enumerate(parts):
        signature = [part['wire_format_schema'], part['credential_channel_config'], 'same remark', '']
        if split:
            signature.append(index)
        assert part['partition_key'] == fingerprint(json.dumps(signature, sort_keys=True, ensure_ascii=False))


def test_spacex_vertex_api_keys_keep_the_preexisting_single_key_partition_identity():
    spec = next(spec for spec in get_format_specs() if spec['schema_config']['type'] == 'vertex_api_key')
    fmt = SimpleNamespace(**spec)
    entries = entries_for('vertex_api_key')
    prior = partition_entries(entries, fmt, site=SimpleNamespace(adapter='silicon-v1'))
    current = partition_entries(entries, fmt, site=SimpleNamespace(adapter='spacex-hub-v1'))
    assert current == prior
