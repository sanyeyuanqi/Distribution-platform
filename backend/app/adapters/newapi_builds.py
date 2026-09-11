"""Origin-bound, reviewed administrator builds that hide their release version.

An asset fingerprint identifies a reviewed client contract, never an upstream
release or an unobserved relay implementation. Public assets are fetched without
credentials on every check, including immediately before each management write.
"""
import hashlib
from html.parser import HTMLParser
from types import MappingProxyType

import httpx

from ..routing_groups import MAX_ROUTING_GROUP_LENGTH
from .newapi_compatibility import RELEASES, ReleaseContract

BUILD_ID = 'build-34b295d77689'
BUILD_REVIEWED_AT = '2026-09-09'
ORIGIN = 'https://sssvip.shop'
FINGERPRINT = '34b295d77689861d54d3080033127dc802b94e7480781eb794ac806eb0975615'
BUILD = MappingProxyType({'id': BUILD_ID, 'label': '三野 · 已核对构建', 'fingerprint': FINGERPRINT})
SCRIPT_PATHS = (
    '/static/js/vendor-tanstack.c30df73066.js', '/static/js/lib-react.c31ec8aa30.js',
    '/static/js/7010.223f603aa4.js', '/static/js/index.585bc7de3f.js',
)
ASSETS = (
    (SCRIPT_PATHS[-1], FINGERPRINT, 66097),
    ('/static/js/async/6173.d9d85aa367.js', '4956b17b3bb981987804cc4a021315dbd6da32033cb2fe0253817d20745b0d91', 258043),
    ('/static/js/async/2793.94975391dc.js', '6e8bb04133be25f97ac206f8eb777daba4a8b4e3cf8797e38734c1b10d4dd67e', 657351),
)
BUILD_CONTRACT = ReleaseContract(version=BUILD_ID, published_at='', permission_model='role_admin',
                                 channel_types=frozenset({1, 3, 14, 20, 24, 33, 41}))
# User-configured local limit, not a claim about the remote database capacity.
# The remote has rejected an 85-character group before; keep its explicit
# rejection separate from this preflight limit. Count Unicode characters and
# commas, not UTF-8 bytes, and never truncate or remove selected groups.
GROUP_MAX_LENGTH = 128
GROUP_TOO_LONG_MESSAGE = f'此站点渠道分组名称合计不能超过 {GROUP_MAX_LENGTH} 个字符（含逗号），请减少所选分组'
REMOTE_GROUP_TOO_LONG_MESSAGE = '目标站点拒绝了过长的渠道分组，请减少所选分组或联系站点提高长度上限'


def group_length_issue(identity, group):
    if identity == BUILD_ID and isinstance(group, str) and len(group) > GROUP_MAX_LENGTH:
        return GROUP_TOO_LONG_MESSAGE
    return None


def get_contract(identity):
    return BUILD_CONTRACT if identity == BUILD_ID else RELEASES.get(identity)


def build_metadata(identity):
    return dict(BUILD) if identity == BUILD_ID else None


def site_verified_build(site):
    cap = getattr(site, 'capabilities', None) or {}
    if (getattr(site, 'adapter', None) != 'new-api-v1'
            or getattr(site, 'base_url', '').rstrip('/') != ORIGIN
            or not getattr(site, 'verified_at', None)
            or cap.get('verification_error') or cap.get('verified_version') != BUILD_ID
            or cap.get('verified_build') != dict(BUILD)):
        return None
    return dict(BUILD)


def site_group_max_length(site):
    return GROUP_MAX_LENGTH if site_verified_build(site) else MAX_ROUTING_GROUP_LENGTH


def single_vertex_api_key_site(site):
    # Authentication refreshes may clear capabilities. They must not regroup
    # existing credentials into a different remote partition.
    return (getattr(site, 'adapter', None) == 'new-api-v1'
            and getattr(site, 'base_url', '').rstrip('/') == ORIGIN)


def build_snapshot(site):
    if getattr(site, 'adapter', None) == 'spacex-hub-v1':
        from .spacex_hub_build import site_verified_build as hub_verified_build
        build = hub_verified_build(site)
    else:
        build = site_verified_build(site)
    return {'site_build_id': build['id']} if build else {}


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
        self.has_base = False

    def handle_starttag(self, tag, attrs):
        if tag == 'base':
            self.has_base = True
        if tag == 'script':
            sources = [value for name, value in attrs if name == 'src']
            if len(sources) == 1:
                self.paths.append(sources[0])
            elif sources:
                self.paths.append(None)


def identify_build(base_url, status, transport):
    from .silicon import RemoteError

    def rejected():
        return RemoteError('站点隐藏版本，当前公开构建未通过已核对的兼容检查', category='protocol_error',
                           reason='unrecognized_build', endpoint='/api/status', method='GET')

    if base_url.rstrip('/') != ORIGIN or not isinstance(status, dict) or status.get('version') != '':
        raise rejected()

    def public_get(path, limit):
        try:
            response = transport('GET', ORIGIN + path, headers={'Accept': '*/*'}, timeout=20)
        except (httpx.TransportError, OSError):
            raise RemoteError('无法核对站点公开构建，请稍后重新验证', category='connection_error',
                              reason='build_unavailable', endpoint='/api/status', method='GET') from None
        except ValueError:
            raise rejected() from None
        if response.status_code != 200 or len(response.content) > limit:
            raise rejected()
        return response.content

    parser = _Scripts()
    try:
        parser.feed(public_get('/', 128 * 1024).decode('utf-8'))
    except (ValueError, UnicodeError):
        raise rejected() from None
    if parser.has_base or tuple(parser.paths) != SCRIPT_PATHS:
        raise rejected()
    # The pinned entry contains the route -> chunk6173 dependency and filename
    # map. Checking both rejects stale chunks left over by a later deployment.
    for path, digest, size in ASSETS:
        data = public_get(path, size)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise rejected()
    return BUILD_ID
