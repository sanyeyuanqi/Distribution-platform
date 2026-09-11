"""A checked seller contract, rather than an unknown version alone, permits USD conversion."""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.adapters.channel_observation import (
    SILICON_COMPACT_CONTRACT,
    checked_conversion,
    extract_usage,
    normalize_usage_observation,
    quota_conversion,
    site_usage_conversion,
)


def metadata(version='v1.0.0-rc.25-fix-41', *, contract=SILICON_COMPACT_CONTRACT, ratio=500000):
    return quota_conversion({'version': version, 'quota_per_unit': ratio,
                             'usd_exchange_rate': 7.3, 'quota_display_type': 'CNY'},
                            adapter_kind='silicon-v1', verified_version=version,
                            protocol_contract=contract)


@pytest.mark.parametrize('version', ['v1.0.0-rc.25-fix-41', 'future-seller-release'])
def test_checked_compact_contract_allows_version_independent_conversion(version):
    conversion = metadata(version, ratio=Decimal('0.10000000000000001'))
    assert conversion['verified_version'] == version
    assert conversion['protocol_contract'] == SILICON_COMPACT_CONTRACT
    assert conversion['quota_per_unit'] == '0.10000000000000001'
    assert checked_conversion(conversion) == conversion
    assert 'usd_exchange_rate' not in conversion
    result = extract_usage({'used_quota': 1000000, 'balance': 7.3}, conversion=metadata(version))
    assert result['used_amount'] == '2' and result['used_amount_unit'] == 'USD'
    assert result['balance_unit'] is None and result['settlement_verified'] is False


@pytest.mark.parametrize('contract', [None, '', 'silicon-seller-compact-v2', 'silicon_seller', True, {}, []])
def test_unknown_version_without_exact_checked_contract_has_no_conversion(contract):
    assert metadata(contract=contract) is None


@pytest.mark.parametrize(('kind', 'version'), [
    ('new-api-v1', 'v1.0.0-rc.35'), ('new-api-v1', 'future-version'),
    ('tcp-red-v1', 'v1.0.0-rc.32-colin'), ('tcp-red-v1', 'future-version'),
    ('unknown', 'future-version'),
])
def test_seller_contract_cannot_authorize_other_adapters(kind, version):
    assert quota_conversion({'version': version, 'quota_per_unit': 500000},
                            adapter_kind=kind, verified_version=version,
                            protocol_contract=SILICON_COMPACT_CONTRACT) is None


@pytest.mark.parametrize('ratio', [None, 0, -1, True, 'NaN'])
def test_contract_still_requires_actual_positive_finite_ratio(ratio):
    assert metadata(ratio=ratio) is None


def test_contract_does_not_override_mismatched_observed_version():
    assert quota_conversion({'version': 'changed', 'quota_per_unit': 500000},
                            adapter_kind='silicon-v1', verified_version='previous',
                            protocol_contract=SILICON_COMPACT_CONTRACT) is None


@pytest.mark.parametrize('version', ['v1.0.0-rc.25-fix-36', 'v1.0.0-rc.25-fix-38'])
def test_legacy_conversion_shape_stays_valid_without_contract(version):
    conversion = metadata(version, contract=None)
    assert 'protocol_contract' not in conversion
    assert checked_conversion(conversion) == conversion
    site = SimpleNamespace(adapter='silicon-v1', verified_at=datetime.now(UTC), capabilities={
        'verified_version': version, 'usage_conversion': conversion,
        'protocol_contract': SILICON_COMPACT_CONTRACT})
    assert site_usage_conversion(site) == conversion
    tampered = {**conversion, 'protocol_contract': 'unrecognized'}
    assert checked_conversion(tampered) is None


@pytest.mark.parametrize('change', ['missing_contract', 'different_contract', 'changed_version', 'changed_adapter',
                                    'unverified', 'failed', 'removed_conversion_contract'])
def test_site_legacy_derivation_requires_matching_checked_contract(change):
    conversion = metadata()
    site = SimpleNamespace(adapter='silicon-v1', verified_at=datetime.now(UTC), capabilities={
        'verified_version': conversion['verified_version'], 'usage_conversion': conversion,
        'protocol_contract': SILICON_COMPACT_CONTRACT})
    assert site_usage_conversion(site) == conversion
    if change == 'missing_contract':
        site.capabilities.pop('protocol_contract')
    elif change == 'different_contract':
        site.capabilities['protocol_contract'] = 'silicon-seller-compact-v2'
    elif change == 'changed_version':
        site.capabilities['verified_version'] = 'new-version'
    elif change == 'changed_adapter':
        site.adapter = 'tcp-red-v1'
    elif change == 'unverified':
        site.verified_at = None
    elif change == 'failed':
        site.capabilities['verification_error'] = {'category': 'protocol_error'}
    else:
        conversion.pop('protocol_contract')
    assert site_usage_conversion(site) is None


def test_historical_conversion_keeps_original_contract_ratio_and_observation_time():
    old = extract_usage({'used_quota': 500000}, conversion=metadata())
    saved = deepcopy(old)
    newer = metadata('future-seller-release', ratio=1000000)
    assert normalize_usage_observation(old, conversion=newer) == saved
    assert old == saved
    legacy = {'used_quota': 0}
    enriched = normalize_usage_observation(legacy, conversion=newer)
    assert enriched['used_amount'] == '0'
    assert enriched['conversion'] == newer and legacy == {'used_quota': 0}
