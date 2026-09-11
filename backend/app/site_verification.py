"""Safe verification diagnostics, separate from verified capability decisions."""
import re
from datetime import datetime, timezone

ERROR_MESSAGES = {
    'authentication_error': '卖家令牌无效或已失效',
    'identity_mismatch': '卖家用户编号与令牌身份不匹配',
    'permission_denied': '卖家账号没有所需接口权限',
    'rate_limited': '目标站点正在限流，请稍后重新验证',
    'connection_error': '无法连接目标站点，请检查网络及站点状态',
    'server_error': '目标站点服务暂时异常',
    'configuration_error': '连接配置或目标地址不符合安全要求',
    'protocol_error': '目标站点响应与已实现的接入协议不一致',
    'business_error': '目标站点拒绝了验证请求',
    'remote_error': '目标站点验证失败，请检查接入配置',
}
REASON_MESSAGES = {
    'tls_certificate_expired': '目标站点的 HTTPS 证书已过期，请联系站点管理员续期证书',
    'tls_certificate_not_yet_valid': '目标站点的 HTTPS 证书尚未生效，请检查系统时间或联系站点管理员',
    'tls_certificate_invalid': '目标站点的 HTTPS 证书验证失败，请联系站点管理员检查证书配置',
    'incompatible_interface': '目标站点关键接口返回结构不兼容，请核对该接口的权限与字段',
    'interface_changed': '目标站点关键接口协议发生变化，请重新验证后重试',
    'permissions_changed': '目标站点接口权限发生变化，请刷新后重试',
    'unsupported_version': '目标站点版本尚未适配，请更新版本支持后重新验证',
    'invalid_version_response': '目标站点未返回有效版本信息，请检查站点接入协议',
    'unrecognized_build': '目标站点隐藏版本，当前构建尚未适配或已改变，请更新接入支持后重新验证',
    'build_unavailable': '无法读取站点公开构建，请稍后重新验证',
    'build_changed': '站点构建与任务核对时不一致，请重新验证并准备任务',
}


def safe_diagnostic(category, *, reason=None, observed_version=None, http_status=None, endpoint=None, method=None):
    category = category if category in ERROR_MESSAGES else 'remote_error'
    result = {'category': category, 'message': ERROR_MESSAGES[category]}
    if reason in REASON_MESSAGES:
        result.update(reason=reason, message=REASON_MESSAGES[reason])
    # Version tags and fixed API paths may be shown; never copy remote bodies,
    # query strings, arbitrary error messages, headers, or credential fragments.
    if (isinstance(observed_version, str) and len(observed_version) <= 80
            and re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*', observed_version)):
        result['observed_version'] = observed_version
    if type(http_status) is int and 100 <= http_status <= 599:
        result['http_status'] = http_status
    if isinstance(endpoint, str) and re.fullmatch(r'/api/[A-Za-z0-9_/-]{1,120}', endpoint) and method in ('GET', 'HEAD'):
        result.update(endpoint=endpoint, method=method)
        if reason == 'incompatible_interface':
            result['message'] += f'（{method} {endpoint}）'
    return result


def verification_state(site):
    cap = (site.capabilities or {}) if site else {}
    diagnostic = cap.get('verification_error')
    if isinstance(diagnostic, dict):
        return 'failed', safe_diagnostic(diagnostic.get('category'), **{
            key: diagnostic.get(key) for key in ('reason', 'observed_version', 'http_status', 'endpoint', 'method')})
    if not site or not site.verified_at:
        return 'unverified', None
    return 'verified', None


def connection_check_result(value):
    """Expose connection diagnostics without publishing capability/remote payloads."""
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        return None
    checked_at = value.get('checked_at')
    try:
        if not isinstance(checked_at, str):
            return None
        timestamp = datetime.fromisoformat(checked_at)
        # utcnow() and the database use naive UTC throughout this application.
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    result = {'ok': value['ok'], 'checked_at': timestamp.isoformat(),
              'version': safe_diagnostic('remote_error', observed_version=value.get('version')).get('observed_version'),
              'endpoints': []}
    endpoints = value.get('endpoints')
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        safe_path = safe_diagnostic('remote_error', endpoint=endpoint, method='GET').get('endpoint')
        if safe_path and safe_path not in result['endpoints']:
            result['endpoints'].append(safe_path)
    if not result['ok']:
        error = value.get('error')
        error = error if isinstance(error, dict) else {}
        result['error'] = safe_diagnostic(error.get('category'), **{key: error.get(key) for key in
            ('reason', 'observed_version', 'http_status', 'endpoint', 'method')})
    return result
