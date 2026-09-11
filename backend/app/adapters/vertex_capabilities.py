"""Reviewed Vertex Claude API-key routing, not upstream account entitlement.

Official rc.2 first routes API-key Claude requests to the Anthropic publisher.
The previous stable release and rc.1 route them to Google instead. Every
rc.2--rc.35 source was checked on 2026-09-08; management support must also be
present in the bundled release manifest. Fork version names alone are not
evidence that their relay implementation kept the same behavior.
"""
from .newapi_compatibility import RELEASES

_REVIEWED_ROUTING_VERSIONS = (
    frozenset(f'v1.0.0-rc.{number}' for number in range(2, 36))
    | {'v1.0.0-rc.19-i18nfix.2'}
)
_UNSUPPORTED_ROUTING_VERSIONS = frozenset({'v0.13.2', 'v1.0.0-rc.1'})


def vertex_claude_api_key_capability(adapter, version):
    """Return a protocol capability without guessing a key's IAM permissions."""
    if adapter != 'new-api-v1' or not isinstance(version, str):
        return 'unknown'
    if version in _UNSUPPORTED_ROUTING_VERSIONS:
        return 'unsupported'
    if version in _REVIEWED_ROUTING_VERSIONS and version in RELEASES:
        return 'supported'
    return 'unknown'
