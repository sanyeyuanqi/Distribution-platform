"""Signed JSON and OAuth forms reach the pinned transport byte-for-byte."""
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from app import network


@pytest.mark.parametrize('body,content_type', [
    (b'{"signed":true,"text":"\\u4e2d"}', 'application/json'),
    (b'grant_type=urn%3Aietf&assertion=a.b%2Bc', 'application/x-www-form-urlencoded'),
])
def test_exact_bytes_body_preserves_signature_and_content_type(monkeypatch, body, content_type):
    sent = []
    sock = SimpleNamespace(settimeout=lambda _: None, connect=lambda _: None, close=lambda: None)
    response = SimpleNamespace(status=200, getheader=lambda name, default: default,
                               read=lambda _: b'{}', getheaders=list)
    connection = SimpleNamespace(request=lambda method, target, **kwargs: sent.append((method, target, kwargs)),
                                 getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr(network, '_resolve', lambda url: (urlsplit(url), 'example.invalid', 80, [(2, 1, 6, ('192.0.2.1', 80))]))
    monkeypatch.setattr(network.socket, 'socket', lambda *args: sock)
    monkeypatch.setattr(network.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    assert network.safe_request('POST', 'http://example.invalid/inference', data=body,
                                headers={'Content-Type': content_type}).status_code == 200
    assert sent[0][2]['body'] == body and sent[0][2]['headers']['Content-Type'] == content_type


@pytest.mark.parametrize('kwargs', [{'json': {}, 'data': b'{}'}, {'data': 'not-bytes'}])
def test_ambiguous_or_mutable_body_rejected_before_network(monkeypatch, kwargs):
    monkeypatch.setattr(network, '_resolve', lambda *_: pytest.fail('Invalid body must not resolve DNS'))
    with pytest.raises(ValueError):
        network.safe_request('POST', 'https://example.invalid/', **kwargs)
