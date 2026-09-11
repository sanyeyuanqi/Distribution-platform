"""Bounded remote requests with DNS validation and a pinned TCP destination.

No environment proxies or redirects are used. TLS still validates the original
hostname while the TCP connection uses an already checked numeric address.
"""
import http.client
import ipaddress
import json as json_module
import socket
import ssl
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .config import settings


def _allowed(host: str, address) -> bool:
    for entry in settings.allowed_private_hosts.split(','):
        entry = entry.strip().lower().rstrip('.')
        if not entry:
            continue
        if entry == host:
            return True
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            pass
    return False


def _resolve(url: str):
    if any(ord(c) <= 32 for c in url) or '\\' in url:
        raise ValueError('Site URL contains unsafe characters')
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or (parsed.scheme == 'http' and not settings.allow_http_sites):
        raise ValueError('Site URL must use HTTPS (HTTP requires explicit server configuration)')
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Site URL must have a hostname and no embedded credentials or fragment')
    try:
        host = parsed.hostname.lower().rstrip('.').encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValueError('Invalid site hostname') from exc
    if '%' in host or len(host) > 253:
        raise ValueError('Invalid site hostname')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError('Site hostname cannot be resolved') from exc
    destinations = []
    for family, socktype, protocol, _, sockaddr in resolved:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global and not _allowed(host, address):
            raise ValueError('Private, local and reserved destinations require an explicit server allowlist')
        destinations.append((family, socktype, protocol, sockaddr))
    if not destinations:
        raise ValueError('Site hostname has no usable addresses')
    return parsed, host, port, destinations


def checked_url(url: str) -> str:
    _resolve(url)
    return url


def validate_remote_url(url: str) -> str:
    parsed, host, port, _ = _resolve(url.strip())
    if parsed.query:
        raise ValueError('Base URL cannot contain query parameters')
    authority = f'[{host}]' if ':' in host else host
    if port != (443 if parsed.scheme == 'https' else 80):
        authority += f':{port}'
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))


def safe_request(method: str, url: str, headers=None, json=None, params=None, timeout=15, data: bytes | None = None) -> httpx.Response:
    if data is not None and (json is not None or not isinstance(data, bytes)):
        raise ValueError('Use either a JSON body or an exact bytes body')
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params, doseq=True)
    parsed, host, port, destinations = _resolve(url)
    request = httpx.Request(method, url)
    connection = http.client.HTTPConnection(host, port=port, timeout=timeout)
    sock = None
    try:
        for family, socktype, protocol, sockaddr in destinations:
            candidate = socket.socket(family, socktype, protocol)
            candidate.settimeout(timeout)
            try:
                candidate.connect(sockaddr)
                sock = candidate
                break
            except OSError:
                candidate.close()
        if sock is None:
            raise OSError('Unable to connect to validated destination')
        if parsed.scheme == 'https':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection.sock = sock
        target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        outbound_headers = {str(k): str(v) for k, v in (headers or {}).items()
                            if str(k).lower() not in ('host', 'content-length', 'connection', 'transfer-encoding', 'accept-encoding')}
        outbound_headers['Accept-Encoding'] = 'identity'
        outbound_headers['Connection'] = 'close'
        body = data
        if json is not None:
            body = json_module.dumps(json, ensure_ascii=False).encode()
            outbound_headers['Content-Type'] = 'application/json'
        connection.request(method.upper(), target, body=body, headers=outbound_headers)
        response = connection.getresponse()
        if response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            raise ValueError('Remote server ignored identity encoding requirement')
        data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('Remote response exceeds size limit')
        return httpx.Response(response.status, content=data, headers=dict(response.getheaders()), request=request)
    except (OSError, http.client.HTTPException) as exc:
        raise httpx.ConnectError('Remote request failed', request=request) from exc
    finally:
        connection.close()
        if sock is not None:
            sock.close()
