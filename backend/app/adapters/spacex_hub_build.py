"""The reviewed SpaceX Hub client contract, bound to its exact public origin."""
import hashlib
from html.parser import HTMLParser
from types import MappingProxyType

import httpx

BUILD_ID = 'spacex-hub-build-48b7afdb3aa4'
BUILD_REVIEWED_AT = '2026-09-10'
ORIGIN = 'https://api.token-spacex.com'
FINGERPRINT = '48b7afdb3aa44c7bbe274231b774564ed155f68d57be9b8be832c315bca1fe87'
BUILD = MappingProxyType({'id': BUILD_ID, 'label': 'SpaceX Hub · 已核对构建', 'fingerprint': FINGERPRINT})
SCRIPT_PATHS = (
    '/static/js/vendor-ui-primitives.0a28e581ca.js',
    '/static/js/vendor-tanstack.74c48f25c5.js',
    '/static/js/lib-react.064bab1680.js',
    '/static/js/4146.2e23e4b9dc.js',
    '/static/js/index.c3891c5ce3.js',
)
ASSETS = (
    (SCRIPT_PATHS[-1], FINGERPRINT, 3667929),
    ('/static/js/async/5457.0c75bd0410.js', 'd5e9482af05b5e94990e193d37b5f6ee91e3f519d3d9cc3af6524406afa0bc63', 119032),
    ('/static/js/async/2438.57a4fda8d6.js', '0d094ab57be112830b4b7ee986b21cc8f806fefab337ec810c3c119a24127946', 74325),
)


def site_verified_build(site):
    cap = getattr(site, 'capabilities', None) or {}
    if (getattr(site, 'adapter', None) != 'spacex-hub-v1'
            or getattr(site, 'base_url', '').rstrip('/') != ORIGIN
            or not getattr(site, 'verified_at', None) or not isinstance(cap, dict)
            or cap.get('verification_error') or cap.get('verified_version') != BUILD_ID
            or cap.get('verified_build') != dict(BUILD)):
        return None
    return dict(BUILD)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths, self.has_base = [], False

    def handle_starttag(self, tag, attrs):
        if tag == 'base':
            self.has_base = True
        if tag == 'script':
            sources = [value for name, value in attrs if name == 'src']
            if sources:
                self.paths.append(sources[0] if len(sources) == 1 else None)


def identify_build(base_url, status, transport):
    from .silicon import RemoteError

    def rejected():
        return RemoteError('SpaceX Hub 公开构建与已核对的接口不一致，请重新验证站点',
                           category='protocol_error', reason='unrecognized_build',
                           endpoint='/api/status', method='GET')

    if base_url.rstrip('/') != ORIGIN or not isinstance(status, dict) or status.get('version') != '':
        raise rejected()

    def public_get(path, limit):
        try:
            response = transport('GET', ORIGIN + path, headers={'Accept': '*/*'}, timeout=20)
        except (httpx.TransportError, OSError):
            raise RemoteError('暂时无法核对 SpaceX Hub 公开构建', category='connection_error',
                              reason='build_unavailable', endpoint='/api/status', method='GET') from None
        except ValueError:
            raise rejected() from None
        # safe_request does not redirect; injected transports must not hide one.
        if (response.status_code != 200 or getattr(response, 'history', None)
                or len(response.content) > limit):
            raise rejected()
        return response.content

    parser = _Scripts()
    try:
        parser.feed(public_get('/', 128 * 1024).decode('utf-8'))
    except (UnicodeError, ValueError):
        raise rejected() from None
    if parser.has_base or tuple(parser.paths) != SCRIPT_PATHS:
        raise rejected()
    # The entry pins both Hub route chunks. Old chunks left on the CDN cannot
    # authorize a newly deployed entry or a different ordered script chain.
    for path, digest, size in ASSETS:
        content = public_get(path, size)
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise rejected()
    return BUILD_ID
