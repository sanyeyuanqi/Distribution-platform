"""Immutable encrypted batches and deterministic, protocol-compatible partitions."""
import json

from sqlalchemy import delete

from .models_channels import ChannelCredential
from .newapi_formats import (
    credential_fingerprint,
    credential_hint,
    credential_wire_schema,
    normalize_credential,
)
from .security import decrypt, fingerprint


def encode_entries(entries):
    return json.dumps({'container': 1, 'entries': [
        {'key': entry['key'], 'remark': entry.get('remark', ''), 'proxy': entry.get('proxy', '')}
        for entry in entries]}, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def channel_entries(channel, *, ciphertext=None):
    value = decrypt(ciphertext or channel.key_encrypted)
    if getattr(channel, 'key_mode', 'single') != 'multiple':
        return [{'key': value, 'remark': channel.remark,
                 'proxy': decrypt(channel.proxy_encrypted) if channel.proxy_encrypted else ''}]
    try:
        data = json.loads(value)
        entries = data['entries']
        if data['container'] != 1 or not isinstance(entries, list) or len(entries) != channel.key_count:
            raise ValueError
        if any(not isinstance(row, dict) or set(row) != {'key', 'remark', 'proxy'}
               or any(not isinstance(value, str) for value in row.values()) for row in entries):
            raise ValueError
        return entries
    except (ValueError, KeyError, TypeError):
        raise ValueError('密钥容器结构或成员数量无效') from None


def entry_protocol(entry, fmt):
    key = normalize_credential(entry['key'], fmt)
    schema = credential_wire_schema(key, fmt.schema_config)
    config = {}
    if fmt.schema_config.get('type') in ('azure_gpt', 'azure_claude'):
        from .azure_credentials import azure_credential_fields
        fields = azure_credential_fields(key, fmt.schema_config['type'])
        schema, config = fields['credential_format'], fields['channel_config']
    return schema, config


def partition_entries(entries, fmt, *, multiple=True, site=None):
    partitions = {}
    for index, entry in enumerate(entries):
        schema, config = entry_protocol(entry, fmt)
        signature = [schema, config, entry.get('remark', ''), entry.get('proxy', '')]
        # Official source proves newline API-key containers work even though
        # its UI hides them. Forks retain their verified single-key contract.
        # Permissions must not change the identity of an existing partition.
        from .adapters.newapi_builds import single_vertex_api_key_site
        official_api_key_container = (site and site.adapter == 'new-api-v1'
            and not single_vertex_api_key_site(site))
        # Hub channels accept one credential for every protocol. Keep this
        # partition rule tied to the adapter, not mutable account permissions.
        single_key_channel = site and site.adapter == 'spacex-hub-v1'
        if single_key_channel or (schema['type'] == 'vertex_api_key' and not official_api_key_container):
            signature.append(index)
        identity = fingerprint(json.dumps(signature, sort_keys=True, ensure_ascii=False)) if multiple else ''
        kind = schema['type']
        label = {'aws_ak_sk': 'Bedrock · AK/SK', 'aws_api_key': 'Bedrock · API Key',
                 'vertex_json': 'Vertex · 服务账号 JSON', 'vertex_api_key': 'Vertex · API Key'}.get(kind, 'API Key')
        if config.get('base_url'):
            from urllib.parse import urlsplit
            label = urlsplit(config['base_url']).hostname or label
        part = partitions.setdefault(identity, {'partition_key': identity, 'partition_label': label,
            'entries': [], 'indices': [], 'wire_format_schema': schema, 'credential_channel_config': config})
        part['entries'].append(entry)
        part['indices'].append(index)
    for part in partitions.values():
        part['key_count'] = len(part['entries'])
        part['member_fingerprints'] = [credential_fingerprint(row['key'], fmt) for row in part['entries']]
    return list(partitions.values())


def channel_partitions(channel, fmt, *, ciphertext=None, site=None):
    return partition_entries(channel_entries(channel, ciphertext=ciphertext), fmt,
                             multiple=getattr(channel, 'key_mode', 'single') == 'multiple', site=site)


def partition_for(channel, fmt, partition_key='', *, ciphertext=None, site=None):
    parts = channel_partitions(channel, fmt, ciphertext=ciphertext, site=site)
    part = next((part for part in parts if part['partition_key'] == partition_key), None)
    if part is None:
        raise ValueError('密钥容器的分发分区已改变，请重新核对')
    return part


def partition_snapshot(channel, fmt, partition_key='', *, site=None):
    if getattr(channel, 'key_mode', 'single') != 'multiple':
        return {}
    part = partition_for(channel, fmt, partition_key, site=site)
    return {'key_mode': 'multiple', 'partition_key': partition_key,
            'partition_label': part['partition_label'], 'key_count': part['key_count'],
            'member_fingerprints': part['member_fingerprints'],
            'wire_format_schema': part['wire_format_schema'],
            'credential_channel_config': part['credential_channel_config']}


def primary_credential(channel, fmt=None, partition_key='', *, site=None):
    if getattr(channel, 'key_mode', 'single') != 'multiple':
        return decrypt(channel.key_encrypted)
    if fmt is None:
        return channel_entries(channel)[0]['key']
    return partition_for(channel, fmt, partition_key, site=site)['entries'][0]['key']


def wire_keys(part, fmt):
    result = []
    for row in part['entries']:
        key = row['key']
        if fmt.schema_config.get('type') in ('azure_gpt', 'azure_claude'):
            from .azure_credentials import azure_credential_fields
            key = azure_credential_fields(key, fmt.schema_config['type'])['key']
        result.append(key)
    return result


def bundle_identity(entries, fmt):
    members = sorted(credential_fingerprint(row['key'], fmt) for row in entries)
    return fingerprint(json.dumps(['credential-container-v1', members], separators=(',', ':')))


def bundle_hint(entries, fmt):
    return f'{len(entries)} 个密钥 · ' + credential_hint(entries[0]['key'], fmt)[:70]


def replace_member_index(db, channel, entries, fmt):
    db.execute(delete(ChannelCredential).where(ChannelCredential.channel_id == channel.id))
    for index, entry in enumerate(entries):
        db.add(ChannelCredential(owner_id=channel.owner_id, channel_id=channel.id, ordinal=index,
                                 fingerprint=credential_fingerprint(entry['key'], fmt)))


def upload_text(channel):
    # Every normalized JSON credential is one compact line; parser handles the
    # business format. Do not reveal the internal encrypted envelope via API.
    return '\n'.join(entry['key'] for entry in channel_entries(channel))


def multikey_supported(site):
    return (site.capabilities or {}).get('multi_key') == 'supported'
