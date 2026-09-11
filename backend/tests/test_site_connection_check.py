"""The connection button cannot change distribution or grant verified capabilities."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.adapters.silicon import RemoteError
from app.db import utcnow
from app.models import Site
from app.routers import sites
from app.security import encrypt
from app.site_verification import connection_check_result, verification_state


@pytest.fixture
def connection_site(db):
    row = Site(name='Connection only', prefix='CONNECTION', base_url='https://fixture.invalid',
        adapter='silicon-v1', seller_user_id='7', token_encrypted=encrypt('private-token'),
        enabled=True, health='server_error', verified_at=utcnow(),
        capabilities={'verified_version': 'v1.0.0', 'can_write': False, 'create': 'permission_denied',
                      'models': ['model-a'], 'groups': ['default'], 'internal': 'never-publish'})
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_connection_preserves_switch_capabilities_and_verification(login, db, monkeypatch, connection_site, enabled, outcome):
    row = connection_site
    row.enabled = enabled
    db.commit()
    before = deepcopy(row.capabilities)
    verified_at = row.verified_at
    calls = []

    def run_check():
        calls.append('check')
        if outcome == 'failure':
            raise RemoteError('private-token', category='authentication_error', status_code=401,
                              endpoint='/api/user/self', method='GET')
        return {'version': 'v9.9.9-future', 'endpoints': ['/api/user/self', '/api/seller/channel/']}

    def forbidden(*args, **kwargs):
        pytest.fail('Connection check must not validate templates or invoke full verification')

    monkeypatch.setattr(sites, 'get_adapter', lambda site: SimpleNamespace(check_connection=run_check, verify=forbidden))
    monkeypatch.setattr(sites, 'distribution_readiness', forbidden)
    monkeypatch.setattr(sites, 'stop_unready_distribution', forbidden)
    response = login('root').post(f'/api/sites/{row.id}/check-connection')
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'id', 'connection_check'}
    check = body['connection_check']
    assert check['ok'] is (outcome == 'success') and check['checked_at']
    if outcome == 'success':
        assert check['version'] == 'v9.9.9-future'
    else:
        assert check['error']['endpoint'] == '/api/user/self'
        assert check['error']['http_status'] == 401
    assert calls == ['check']
    assert 'private-token' not in response.text and 'never-publish' not in response.text
    db.refresh(row)
    assert row.enabled is enabled and row.health == 'server_error' and row.verified_at == verified_at
    assert row.capabilities == {**before, 'connection_check': check}


def test_successful_connection_does_not_turn_failed_full_verification_into_ready(login, db, monkeypatch, connection_site):
    row = connection_site
    row.verified_at = None
    row.enabled = False
    row.capabilities = {'verification_error': {'category': 'protocol_error', 'reason': 'unsupported_version'}}
    db.commit()
    monkeypatch.setattr(sites, 'get_adapter', lambda site: SimpleNamespace(
        check_connection=lambda: {'version': 'v9.9.9', 'endpoints': ['/api/user/self']}))
    root = login('root')
    assert root.post(f'/api/sites/{row.id}/check-connection').json()['connection_check']['ok'] is True
    db.refresh(row)
    assert verification_state(row)[0] == 'failed' and row.verified_at is None and row.enabled is False
    projection = root.get('/api/sites').json()['items'][0]
    assert projection['connection_check']['ok'] is True
    assert projection['distribution_ready'] is False
    assert projection['template_options']['models'] == []
    assert 'capabilities' not in projection


@pytest.mark.parametrize('role', ['admin', 'user'])
def test_only_root_can_check_or_view_connection_details(login, db, monkeypatch, connection_site, role):
    def forbidden(*args, **kwargs):
        pytest.fail('Non-root user must not contact remote interfaces')
    monkeypatch.setattr(sites, 'get_adapter', forbidden)
    client = login(role)
    assert client.post(f'/api/sites/{connection_site.id}/check-connection').status_code == 403
    assert 'connection_check' not in client.get('/api/sites').json()['items'][0]


def test_projection_only_exposes_safe_connection_diagnostics():
    result = connection_check_result({'ok': False, 'checked_at': utcnow().isoformat(),
        'version': 'v1.0.0?token=private-token', 'endpoints': ['/api/status', '/api/status?key=private-token'],
        'secret': 'private-token', 'error': {'category': 'authentication_error', 'message': 'private-token',
        'endpoint': '/api/user/self?token=private-token', 'method': 'GET', 'http_status': 401}})
    assert result['version'] is None and result['endpoints'] == ['/api/status']
    assert 'private-token' not in str(result)
    assert result['error']['http_status'] == 401 and 'endpoint' not in result['error']
    assert connection_check_result({'ok': 'true', 'checked_at': utcnow().isoformat()}) is None
    assert connection_check_result({'ok': True, 'checked_at': 'invalid-date'}) is None
