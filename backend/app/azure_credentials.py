"""Fixed Azure upload credentials and their New API channel fields.

The resource is the Azure endpoint's custom subdomain, not a deployment name.
Composite upload credentials belong in encrypted storage; only ``key`` from
``azure_credential_fields`` is sent as the remote channel credential.

Contracts: Microsoft Foundry custom subdomains and Claude Foundry authentication,
plus New API's Azure (3) and Anthropic (14) channel adapters. Claude accepts
``x-api-key`` and appends ``/v1/messages`` to the generated Anthropic base URL.
"""

import re
from datetime import date

_KINDS = {'azure_gpt': 3, 'azure_claude': 14}
_RESOURCE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,62}[a-zA-Z0-9]')
_API_VERSION = re.compile(r'\d{4}-\d{2}-\d{2}(?:-preview)?', flags=re.ASCII)
_MAX_CREDENTIAL_LENGTH = 4096


def normalize_azure_credential(value, kind):
    """Validate a supported composite credential without exposing it in errors."""
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError('不支持的 Azure 密钥类型')
    if not isinstance(value, str) or not value or len(value) > _MAX_CREDENTIAL_LENGTH:
        raise ValueError('Azure 凭据不能为空，且不能超过 4096 个字符')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Azure 凭据不能包含换行或控制字符')

    parts = [part.strip() for part in value.split('|')]
    allowed_parts = (3,) if kind == 'azure_gpt' else (2, 3)
    if len(parts) not in allowed_parts or not all(parts):
        expected = '资源名|APIKey|APIVersion'
        raise ValueError(f'Azure 凭据格式应为 {expected}，不能有空字段')

    resource, key = parts[:2]
    if _RESOURCE.fullmatch(resource) is None:
        raise ValueError('Azure 资源名须为 2–64 位字母、数字或连字符，首尾为字母或数字，请勿填写网址或路径')
    if any(not ('!' <= char <= '~') for char in key):
        raise ValueError('Azure API Key 不能包含空白、控制字符或非 ASCII 字符')

    if len(parts) == 3:
        version = parts[2]
        if _API_VERSION.fullmatch(version) is None:
            raise ValueError('Azure API 版本格式应为 YYYY-MM-DD 或 YYYY-MM-DD-preview')
        try:
            date.fromisoformat(version[:10])
        except ValueError:
            raise ValueError('Azure API 版本必须包含有效日期') from None

    parts[0] = resource.lower()
    return '|'.join(parts)


def azure_credential_fields(value, kind):
    """Derive a fixed official endpoint and split the opaque API key for dispatch.

    ``other`` holds the legacy Azure deployment API version. New API handles its
    separate Azure Responses API version independently; this is not a URL suffix.
    Claude accepts an optional third input segment for a uniform paste format,
    but that Azure API version is not its ``anthropic-version`` header and is
    never forwarded to the Anthropic-compatible endpoint.
    """
    parts = normalize_azure_credential(value, kind).split('|')
    resource, key = parts[:2]
    base_url = (f'https://{resource}.openai.azure.com' if kind == 'azure_gpt'
                else f'https://{resource}.services.ai.azure.com/anthropic')
    return {
        'key': key,
        'credential_format': {'type': 'api_key', 'remote_type': _KINDS[kind]},
        'channel_config': {'base_url': base_url, 'other': parts[2] if kind == 'azure_gpt' else ''},
    }
