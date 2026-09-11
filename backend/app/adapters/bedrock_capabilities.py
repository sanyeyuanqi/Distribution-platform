"""Pinned AWS SDK compatibility path for official NewAPI Bedrock API keys.

In the reviewed releases the explicit ``api_key`` branch builds an incorrect
runtime URL and forwards the pipe-delimited key as its bearer token. The
default SDK branch instead selects Bearer authentication for two credential
parts and SigV4 for three parts, taking the Region from the selected key.
Controller and settings validation permit both single and multi-key input.
This gate concerns transport configuration, not credential authorization.
"""
from .newapi_compatibility import RELEASES

_REVIEWED_SDK_VERSIONS = (
    frozenset({'v0.13.2', 'v1.0.0-rc.19-i18nfix.2'})
    | frozenset(f'v1.0.0-rc.{number}' for number in range(11, 36))
)


def bedrock_api_key_sdk_mode(adapter, version):
    """Whether an APIKey|Region credential should use the reviewed SDK path."""
    return (
        adapter == 'new-api-v1'
        and isinstance(version, str)
        and version in _REVIEWED_SDK_VERSIONS
        and version in RELEASES
    )
