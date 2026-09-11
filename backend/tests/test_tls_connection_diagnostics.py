"""TLS certificate failures remain verified, classified, and free of raw error text."""
# ruff: noqa: F811
import ssl
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from app import network
from app.adapters.new_api import NewAPIAdapter
from app.adapters.silicon import RemoteError, SiliconAdapter, _tls_certificate_reason
from app.routers import sites
from app.security import encrypt
from app.site_verification import REASON_MESSAGES, connection_check_result
from test_site_connection_check import connection_site  # noqa: F401


def certificate_error(code):
    error = ssl.SSLCertVerificationError(1, 'raw-sensitive-token certificate diagnostic')
    if code is not None:
        error.verify_code = code
    error.verify_message = 'raw-sensitive-token arbitrary verify message'
    return error


@pytest.fixture
def failing_tls(monkeypatch):
    state = SimpleNamespace(error=certificate_error(10), connects=[], handshakes=[], sends=[])
    sock = SimpleNamespace(settimeout=lambda _: None, connect=state.connects.append, close=lambda: None)
    connection = SimpleNamespace(request=lambda *args, **kwargs: state.sends.append(args), close=lambda: None)
    monkeypatch.setattr(network, '_resolve', lambda url: (
        urlsplit(url), 'fixture.invalid', 443, [(2, 1, 6, ('192.0.2.1', 443))]))
    # Replace only this module's socket reference; TestClient's event loop still
    # needs real local socketpairs and must not inherit our outbound TLS mock.
    monkeypatch.setattr(network, 'socket', SimpleNamespace(socket=lambda *args: sock))
    monkeypatch.setattr(network.http.client, 'HTTPConnection', lambda *args, **kwargs: connection)
    original_context = network.ssl.create_default_context

    def context():
        # The real default policy is still hostname verification + required certificates.
        actual = original_context()
        assert actual.check_hostname and actual.verify_mode == ssl.CERT_REQUIRED

        def wrap(actual_sock, *, server_hostname):
            assert actual_sock is sock and server_hostname == 'fixture.invalid'
            state.handshakes.append(server_hostname)
            raise state.error

        return SimpleNamespace(wrap_socket=wrap)

    monkeypatch.setattr(network.ssl, 'create_default_context', context)
    return state


def adapter(adapter_type=NewAPIAdapter, **options):
    site = SimpleNamespace(base_url='https://fixture.invalid', token_encrypted=encrypt('private-token'),
                           seller_user_id='7')
    return adapter_type(site, **options)


@pytest.mark.parametrize(('code', 'reason'), [
    (10, 'tls_certificate_expired'), (9, 'tls_certificate_not_yet_valid'),
    (18, 'tls_certificate_invalid'), (62, 'tls_certificate_invalid'),
    (None, 'tls_certificate_invalid'), ('10', 'tls_certificate_invalid'),
])
def test_real_safe_request_wrapper_preserves_certificate_cause_and_check_classifies_it(failing_tls, code, reason):
    failing_tls.error = certificate_error(code)
    with pytest.raises(httpx.ConnectError) as wrapped:
        network.safe_request('GET', 'https://fixture.invalid/api/user/self')
    assert wrapped.value.__cause__ is failing_tls.error
    assert _tls_certificate_reason(wrapped.value) == reason
    failing_tls.connects.clear()
    failing_tls.handshakes.clear()
    with pytest.raises(RemoteError) as caught:
        adapter().check_connection()
    error = caught.value
    assert error.category == 'connection_error' and error.reason == reason
    assert error.endpoint == '/api/user/self' and error.method == 'GET'
    assert error.retryable and not error.unknown and error.status_code is None
    assert len(failing_tls.connects) == len(failing_tls.handshakes) == 1
    assert not failing_tls.sends and 'raw-sensitive-token' not in str(error)


def test_nested_explicit_causes_are_supported_without_unbounded_cycles_or_text_matching():
    inner = httpx.ConnectError('raw-sensitive-token')
    inner.__cause__ = certificate_error(10)
    outer = httpx.ConnectError('unrelated wrapper')
    outer.__cause__ = inner
    assert _tls_certificate_reason(outer) == 'tls_certificate_expired'
    for error in (httpx.ConnectError('certificate has expired'), ssl.SSLError('certificate has expired')):
        assert _tls_certificate_reason(error) is None
    outer.__cause__ = inner
    inner.__cause__ = outer
    assert _tls_certificate_reason(outer) is None


def test_certificate_failure_never_retries_or_marks_business_write_as_confirmed(failing_tls):
    before = []
    with pytest.raises(RemoteError) as caught:
        adapter(SiliconAdapter, before_write=lambda: before.append('guard')).request(
            'POST', '/api/seller/channel/', body={'key': 'not-sent-fixture-key'})
    assert before == ['guard']
    assert caught.value.unknown and not caught.value.retryable
    assert caught.value.reason == 'tls_certificate_expired'
    assert len(failing_tls.handshakes) == 1 and not failing_tls.sends


@pytest.mark.parametrize(('code', 'reason', 'enabled'), [
    (10, 'tls_certificate_expired', True), (10, 'tls_certificate_expired', False),
    (9, 'tls_certificate_not_yet_valid', True), (18, 'tls_certificate_invalid', True),
])
def test_api_reports_safe_certificate_reason_without_changing_capabilities_or_distribution_switch(
        db, login, monkeypatch, connection_site, failing_tls, code, reason, enabled):
    row = connection_site
    row.adapter, row.enabled = 'new-api-v1', enabled
    db.commit()
    before, verified_at, health = deepcopy(row.capabilities), row.verified_at, row.health
    failing_tls.error = certificate_error(code)
    monkeypatch.setattr(sites, 'get_adapter', NewAPIAdapter)
    client = login('root')

    def forbidden(*args, **kwargs):
        pytest.fail('Connection diagnostics must not reverify templates or change distribution')

    with monkeypatch.context() as scoped:
        scoped.setattr(sites, 'distribution_readiness', forbidden)
        scoped.setattr(sites, 'stop_unready_distribution', forbidden)
        scoped.setattr(NewAPIAdapter, 'permissions', forbidden)
        scoped.setattr(NewAPIAdapter, '_version', forbidden)
        response = client.post(f'/api/sites/{row.id}/check-connection')
    assert response.status_code == 200, response.text
    check = response.json()['connection_check']
    assert check['ok'] is False
    assert check['error'] == {'category': 'connection_error', 'reason': reason,
        'message': REASON_MESSAGES[reason], 'endpoint': '/api/user/self', 'method': 'GET'}
    assert 'raw-sensitive-token' not in response.text and 'private-token' not in response.text
    assert len(failing_tls.handshakes) == 1 and not failing_tls.sends
    db.refresh(row)
    assert row.enabled is enabled and row.verified_at == verified_at and row.health == health
    assert row.capabilities == {**before, 'connection_check': check}
    projected = client.get('/api/sites').json()['items'][0]
    assert projected['connection_check'] == check
    assert 'capabilities' not in projected


def test_diagnostic_projection_ignores_exception_text_even_for_known_certificate_reason():
    value = {'ok': False, 'checked_at': '2026-09-10T01:30:00Z', 'error': {
        'category': 'connection_error', 'reason': 'tls_certificate_expired',
        'message': 'raw-sensitive-token', 'verify_message': 'raw-sensitive-token',
        'endpoint': '/api/user/self', 'method': 'GET'}}
    result = connection_check_result(value)
    assert result['error']['message'] == REASON_MESSAGES['tls_certificate_expired']
    assert 'raw-sensitive-token' not in str(result)
