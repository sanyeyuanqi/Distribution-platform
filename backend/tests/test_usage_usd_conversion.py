"""Remote quota conversion uses observed USD units, never billing or balance guesses."""
# ruff: noqa: F811
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters.channel_observation import (
    extract_usage,
    normalize_usage_observation,
    quota_conversion,
    site_usage_conversion,
)
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.adapters.tcp_red import TcpRedAdapter
from app.db import utcnow
from app.distribution_monitoring import observation_json
from app.models import Site
from app.models_billing import UsageFact
from app.models_channels import Distribution, Task
from app.security import encrypt
from sqlalchemy import func, select
from test_channel_monitoring import monitor_action, observed, row  # noqa: F401
from test_channels import delete_case, setup_catalog  # noqa: F401
from test_newapi_adapter import RemoteFixture


def conversion(ratio=500000):
    return quota_conversion({'version': TcpRedAdapter.VERSION, 'quota_per_unit': ratio},
        adapter_kind='tcp-red-v1', verified_version=TcpRedAdapter.VERSION)


@pytest.mark.parametrize(('quota', 'ratio', 'expected'), [
    (0, 500000, '0'), (1234567, 500000, '2.469134'), (1, 1000000000, '0.000000001'),
    (9007199254740993, 10, '900719925474099.3'), (3, Decimal('0.0000000003'), '10000000000'),
])
def test_decimal_conversion_preserves_zero_large_integers_and_small_values(quota, ratio, expected):
    result = extract_usage({'used_quota': quota, 'balance': 7.3}, conversion=conversion(ratio))
    assert result['used_amount'] == expected and result['used_amount_unit'] == 'USD'
    assert result['used_quota'] == quota and result['quota_unit'] == 'quota'
    assert result['balance'] == 7.3 and result['balance_unit'] is None
    assert result['settlement_verified'] is False and result['conversion_status'] == 'available'


@pytest.mark.parametrize('ratio', [None, 0, -1, True, False, '', 'not a ratio', float('inf'), float('nan'), {}, [], '1e999'])
def test_missing_invalid_ratio_never_uses_default_even_for_zero(ratio):
    metadata = conversion(ratio)
    assert metadata is None
    result = extract_usage({'used_quota': 0}, conversion=metadata)
    assert result['used_amount'] is None and result['used_amount_unit'] is None
    assert result['conversion_status'] == 'unavailable' and '比例' in result['conversion_message']


def test_display_currency_price_and_exchange_rate_do_not_change_usd():
    metadata = quota_conversion({'version': TcpRedAdapter.VERSION, 'quota_per_unit': 200000,
        'quota_display_type': 'CNY', 'price': 99, 'usd_exchange_rate': 7.3},
        adapter_kind='tcp-red-v1', verified_version=TcpRedAdapter.VERSION)
    assert extract_usage({'used_quota': 400000}, conversion=metadata)['used_amount'] == '2'
    assert 'price' not in metadata and 'usd_exchange_rate' not in metadata


@pytest.mark.parametrize('profile', [('tcp-red-v1', 'future-version'), ('unknown', TcpRedAdapter.VERSION),
    ('silicon-v1', 'v1.0.0-rc.25-fix-22-multiseller-2'), ('new-api-v1', 'v1.0.0-rc.99')])
def test_unknown_versions_or_protocols_have_no_trusted_conversion(profile):
    kind, version = profile
    assert quota_conversion({'version': version, 'quota_per_unit': 500000},
                            adapter_kind=kind, verified_version=version) is None


@pytest.mark.parametrize('version', [None, 'v1.0.0-rc.35', 'v1.0.0-rc.32-colin-future'])
def test_status_version_must_match_exact_verified_profile(version):
    assert quota_conversion({'version': version, 'quota_per_unit': 500000},
        adapter_kind='tcp-red-v1', verified_version=TcpRedAdapter.VERSION) is None


def test_missing_counter_is_not_fabricated_as_zero_dollars():
    result = extract_usage({'balance': 0}, conversion=conversion())
    assert result['used_amount'] is None and result['conversion'] is not None
    assert '消耗额度' in result['conversion_message']


def test_old_snapshot_can_derive_from_verified_metadata_without_mutation_or_overwriting_old_ratio():
    old = {'used_quota': 500000, 'balance': 0, 'balance_updated_at': 0}
    before = deepcopy(old)
    first = normalize_usage_observation(old, conversion=conversion(500000))
    assert first['used_amount'] == '1' and old == before
    newer = normalize_usage_observation(first, conversion=conversion(1000000))
    assert newer == first
    unavailable = extract_usage({'used_quota': 500000})
    assert normalize_usage_observation(unavailable, conversion=conversion())['used_amount'] is None


@pytest.mark.parametrize('change', ['unverified', 'failed', 'changed_version', 'changed_adapter'])
def test_legacy_derivation_requires_matching_verified_site(change):
    site = SimpleNamespace(adapter='tcp-red-v1', verified_at=utcnow(), capabilities={
        'verified_version': TcpRedAdapter.VERSION, 'usage_conversion': conversion()})
    assert site_usage_conversion(site) is not None
    if change == 'unverified': site.verified_at = None
    elif change == 'failed': site.capabilities['verification_error'] = {'category': 'protocol_error'}
    elif change == 'changed_version': site.capabilities['verified_version'] = 'other'
    else: site.adapter = 'new-api-v1'
    assert site_usage_conversion(site) is None


@pytest.mark.parametrize(('kind', 'version', 'adapter_type'), [
    ('tcp-red-v1', TcpRedAdapter.VERSION, TcpRedAdapter), ('silicon-v1', SiliconAdapter.VERSION, SiliconAdapter),
    ('silicon-v1', 'v1.0.0-rc.25-fix-38', SiliconAdapter), ('new-api-v1', 'v0.13.2', NewAPIAdapter),
    ('new-api-v1', 'v1.0.0-rc.35', NewAPIAdapter),
])
def test_all_reviewed_adapters_verify_and_sync_with_actual_status_ratio(kind, version, adapter_type):
    server = RemoteFixture(version)
    server.remote.update(created_by=71, used_quota=1234567, balance=7.3)
    writes = []
    def handle(request):
        if request.url.path == '/api/status':
            # Keep a non-binary-exact decimal token to exercise status parsing.
            return httpx.Response(200, content='{"success":true,"data":{"version":"' + version +
                '","quota_per_unit":0.10000000000000001,"usd_exchange_rate":7.3}}')
        return server.handle(request)
    site = SimpleNamespace(adapter=kind, base_url='https://fixture.invalid', seller_user_id='71',
                           token_encrypted=encrypt('fixture-token'))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        adapter = adapter_type(site, transport=client.request, before_write=lambda: writes.append(True))
        cap = adapter.verify()['capabilities']
        assert cap['usage_conversion']['quota_per_unit'] == '0.10000000000000001'
        result = adapter.usage('91', expected={'id': '91', 'name': 'stable-name', 'type': 1})
        assert result['conversion']['quota_per_unit'] == '0.10000000000000001'
        assert result['used_amount_unit'] == 'USD' and result['balance_unit'] is None
    assert not writes and not server.writes


def test_manual_sync_persists_dollars_and_failure_preserves_prior_ratio_without_bill_facts(db, observed):
    observed.usage_value = extract_usage({'used_quota': 750000, 'balance': 12.5}, conversion=conversion())
    monitor_action(db, observed, 'sync_usage')
    assert worker.run_once(client=object())
    original = row(observed)['usage_sync']
    assert original['remote_usage']['used_amount'] == '1.5'
    observed.usage_error = RemoteError('metadata unavailable')
    monitor_action(db, observed, 'sync_usage')
    assert worker.run_once(client=object())
    failed = row(observed)['usage_sync']
    assert failed['status'] == 'failed' and failed['remote_usage'] == original['remote_usage']
    assert failed['synced_at'] == original['synced_at']
    assert db.scalar(select(func.count()).select_from(UsageFact)) == 0


def test_site_sync_uses_its_current_ratio_and_get_enriches_legacy_zero(db, users, observed):
    site = db.get(Site, observed.dist.site_id)
    metadata = conversion(1000000)
    site.adapter, site.verified_at = 'tcp-red-v1', utcnow()
    site.capabilities = {**site.capabilities, 'verified_version': TcpRedAdapter.VERSION, 'usage_conversion': metadata}
    observed.dist.remote_snapshot = {**observed.dist.remote_snapshot, '_monitoring': {'usage_sync': {
        'status': 'succeeded', 'synced_at': '2026-09-01T00:00:00Z', 'remote_usage': {'used_quota': 0}}}}
    db.commit()
    assert row(observed)['usage_sync']['remote_usage']['used_amount'] == '0'
    assert 'conversion' not in db.get(Distribution, observed.dist.id).remote_snapshot['_monitoring']['usage_sync']['remote_usage']
    adapter = SimpleNamespace(channels=lambda: [{**observed.remote, 'used_quota': 2000000}],
        usage_conversion=lambda **kwargs: metadata)
    task = db.scalar(select(Task))
    worker.sync_site(db, adapter, task, SimpleNamespace(snapshot={'owner_ids': [users['user'].id]}), users['root'], site)
    db.commit()
    saved = observation_json(db.get(Distribution, observed.dist.id), 'sync_usage')['remote_usage']
    assert saved['used_amount'] == '2' and saved['conversion']['quota_per_unit'] == '1000000'
    assert db.scalar(select(func.count()).select_from(UsageFact)) == 0
