"""Safe, non-settlement observations from reviewed New API channel contracts."""
import math
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

SILICON_COMPACT_CONTRACT = 'silicon-seller-compact-v1'


def _number(value, *, integer=False, nonnegative=False):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite or (nonnegative and value < 0):
        return None
    return value


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if len(str(value)) > 100:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and abs(result.adjusted()) <= 100 else None


def _decimal_text(value):
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def quota_conversion(status, *, adapter_kind, verified_version, protocol_contract=None):
    """Reviewed profiles express quota_per_unit as quota per USD, not FX."""
    from .newapi_builds import BUILD_ID
    from .newapi_compatibility import RELEASES

    if not isinstance(adapter_kind, str) or not isinstance(verified_version, str):
        return None
    compact_seller = adapter_kind == 'silicon-v1' and protocol_contract == SILICON_COMPACT_CONTRACT
    if protocol_contract is not None and not compact_seller:
        return None
    supported = (adapter_kind == 'new-api-v1' and (verified_version in RELEASES or verified_version == BUILD_ID)
        or adapter_kind == 'tcp-red-v1' and verified_version == 'v1.0.0-rc.32-colin'
        or adapter_kind == 'silicon-v1' and (compact_seller or verified_version in
            ('v1.0.0-rc.25-fix-36', 'v1.0.0-rc.25-fix-38')))
    reported_version = '' if adapter_kind == 'new-api-v1' and verified_version == BUILD_ID else verified_version
    if not supported or not isinstance(status, dict) or status.get('version') != reported_version:
        return None
    ratio = _decimal(status.get('quota_per_unit'))
    if ratio is None or ratio <= 0:
        return None
    result = {'source': '/api/status.quota_per_unit', 'quota_per_unit': _decimal_text(ratio),
            'base_unit': 'USD', 'adapter_kind': adapter_kind, 'verified_version': verified_version,
            'observed_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z')}
    if compact_seller:
        result['protocol_contract'] = SILICON_COMPACT_CONTRACT
    return result


def checked_conversion(value):
    from .newapi_builds import BUILD_ID
    if (not isinstance(value, dict) or value.get('source') != '/api/status.quota_per_unit'
            or value.get('base_unit') != 'USD'):
        return None
    reported_version = '' if value.get('adapter_kind') == 'new-api-v1' and value.get('verified_version') == BUILD_ID else value.get('verified_version')
    verified = quota_conversion({'version': reported_version, 'quota_per_unit': value.get('quota_per_unit')},
        adapter_kind=value.get('adapter_kind'), verified_version=value.get('verified_version'),
        protocol_contract=value.get('protocol_contract'))
    timestamp = value.get('observed_at')
    if not verified or not isinstance(timestamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[\d:.]+Z', timestamp):
        return None
    return {**verified, 'observed_at': timestamp}


def site_usage_conversion(site):
    from .newapi_builds import BUILD_ID, site_verified_build
    cap = getattr(site, 'capabilities', None) or {}
    if not getattr(site, 'verified_at', None) or cap.get('verification_error'):
        return None
    if cap.get('verified_version') == BUILD_ID and not site_verified_build(site):
        return None
    conversion = checked_conversion(cap.get('usage_conversion'))
    if (not conversion or conversion['verified_version'] != cap.get('verified_version')
            or conversion['adapter_kind'] != getattr(site, 'adapter', None)
            or ('protocol_contract' in conversion
                and conversion['protocol_contract'] != cap.get('protocol_contract'))):
        return None
    return conversion


def extract_usage(remote, *, conversion=None):
    """Keep original units; a missing counter is unknown rather than zero."""
    source = remote if isinstance(remote, dict) else {}
    result = {
        'used_quota': _number(source.get('used_quota'), integer=True, nonnegative=True),
        'balance': _number(source.get('balance')),
        'balance_updated_at': _number(source.get('balance_updated_time'), integer=True, nonnegative=True),
        'source': 'channel_detail', 'quota_unit': 'quota', 'balance_unit': None,
        'settlement_verified': False,
    }
    converted = checked_conversion(conversion)
    amount = None
    if converted is not None and result['used_quota'] is not None:
        quota = Decimal(result['used_quota'])
        ratio = Decimal(converted['quota_per_unit'])
        with localcontext() as context:
            context.prec = max(78, len(quota.as_tuple().digits) + len(ratio.as_tuple().digits) + 30)
            amount = _decimal_text(quota / ratio)
    result.update(used_amount=amount, used_amount_unit='USD' if amount is not None else None,
                  conversion=converted, conversion_status='available' if amount is not None else 'unavailable',
                  conversion_message=('已按站点额度比例换算为美元（不作为结算依据）' if amount is not None
                      else '站点未提供可核实的美元换算比例，保留原始额度' if converted is None
                      else '站点未返回有效消耗额度，无法换算美元'))
    return result


def normalize_usage_observation(usage, *, conversion=None):
    """Keep an observation's original ratio; only legacy snapshots may use new metadata."""
    if not isinstance(usage, dict):
        return None
    selected = usage.get('conversion') if 'conversion' in usage else conversion
    return extract_usage({'used_quota': usage.get('used_quota'), 'balance': usage.get('balance'),
                          'balance_updated_time': usage.get('balance_updated_at')}, conversion=selected)


def test_result(payload):
    """Never expose upstream messages, request bodies, keys or provider errors."""
    result = {'success': payload['success'],
              'message': '所选模型测试通过' if payload['success'] else '所选模型测试未通过'}
    data = payload.get('data')
    latency = _number(data.get('response_time'), nonnegative=True) if isinstance(data, dict) else None
    if latency is None:
        seconds = _number(payload.get('time'), nonnegative=True)
        if seconds is not None and seconds <= 86_400:
            latency = seconds * 1000
    if latency is not None and latency <= 86_400_000:
        result['latency_ms'] = round(latency)
    return result
