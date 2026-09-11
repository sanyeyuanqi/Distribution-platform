import socket

import pytest
from app.bootstrap import seed_catalog
from app.config import settings
from app.network import checked_url
from app.security import decrypt, encrypt, fingerprint, mask


def test_role_creation_and_immutable_parent(login, users, db):
    root = login('root')
    created = root.post('/api/users', json={'username': 'new_admin', 'nickname': 'Admin', 'password': 'long-password-123!'})
    assert created.status_code == 201
    assert created.json()['role'] == 'admin'
    admin = login('admin')
    child = root.post('/api/users', json={'username': 'new_child', 'nickname': 'Child', 'password': 'long-password-123!',
                                        'role': 'user'})
    assert child.status_code == 201
    assert child.json()['role'] == 'user'
    assert child.json()['parent_id'] == users['root'].id
    assert root.patch('/api/users/' + child.json()['id'], json={'parent_id': users['other_admin'].id}).status_code == 422
    assert root.patch('/api/users/' + child.json()['id'], json={'username': 'renamed'}).status_code == 422
    assert admin.patch('/api/users/' + child.json()['id'], json={'nickname': 'Forbidden'}).status_code == 404
    assert admin.post('/api/users', json={'username': 'escalated', 'nickname': 'No', 'password': 'long-password-123!', 'role': 'superadmin'}).status_code == 403
    assert login('user').post('/api/users', json={'username': 'forbidden', 'nickname': 'No', 'password': 'long-password-123!'}).status_code == 403


def test_cross_admin_account_isolation(login, users):
    admin = login('admin')
    rows = admin.get('/api/users').json()['items']
    assert {r['id'] for r in rows} == {users['user'].id, users['sibling'].id}
    assert admin.patch('/api/users/' + users['other_user'].id, json={'nickname': 'hacked'}).status_code == 404
    assert admin.delete('/api/users/' + users['other_admin'].id).status_code == 404
    assert all('password_hash' not in r and 'password' not in r for r in rows)


def test_csrf_and_origin_are_enforced(login):
    client = login('user')
    token = client.headers.pop('X-CSRF-Token')
    assert client.patch('/api/auth/profile', json={'nickname': 'new'}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    assert client.patch('/api/auth/profile', json={'nickname': 'new'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.patch('/api/auth/profile', json={'nickname': 'new'}).status_code == 200
    assert client.get('/api/auth/me').headers['cache-control'] == 'no-store'


def test_disabled_user_session_revoked_without_cascade(login, users):
    child = login('user')
    admin = login('admin')
    assert admin.patch('/api/users/' + users['user'].id, json={'active': False}).status_code == 200
    assert child.get('/api/auth/me').status_code == 401
    assert admin.get('/api/auth/me').status_code == 200
    assert login('sibling').get('/api/auth/me').status_code == 200


def test_password_reset_revokes_existing_sessions(login, users):
    child = login('user')
    admin = login('admin')
    assert admin.patch('/api/users/' + users['user'].id, json={'password': 'abc123'}).status_code == 200
    assert child.get('/api/auth/me').status_code == 401
    response = child.post('/api/auth/login', json={'username': 'user', 'password': 'test-password-123!'})
    assert response.status_code == 401
    response = child.post('/api/auth/login', json={'username': 'user', 'password': 'abc123'})
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']


def test_profile_password_changes_revoke_other_sessions(login):
    first, second = login('user'), login('user')
    response = first.patch('/api/auth/profile', json={'current_password': 'test-password-123!', 'password': 'xyz789'})
    assert response.status_code == 200
    assert response.json()['csrf_token'] != first.headers['X-CSRF-Token']
    first.headers['X-CSRF-Token'] = response.json()['csrf_token']
    assert first.get('/api/auth/me').status_code == 200
    assert second.get('/api/auth/me').status_code == 401


def test_login_throttled(client, users):
    for _ in range(10):
        assert client.post('/api/auth/login', json={'username': 'user', 'password': 'wrong'}).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'user', 'password': 'wrong'}).status_code == 429


def test_archive_admin_requires_archiving_children(login, users):
    root = login('root')
    assert root.delete('/api/users/' + users['admin'].id).status_code == 409
    assert root.delete('/api/users/' + users['user'].id).status_code == 200
    assert root.delete('/api/users/' + users['sibling'].id).status_code == 200
    assert root.delete('/api/users/' + users['admin'].id).status_code == 200


def test_cipher_and_mask_never_store_plaintext():
    value = 'secret-credential-0123456789'
    cipher = encrypt(value)
    assert value not in cipher
    assert decrypt(cipher) == value
    assert fingerprint(value) != value
    assert mask(value) != value
    assert mask('short') == '••••'


def test_ssrf_rejects_private_dns_and_redirect_components(monkeypatch):
    monkeypatch.setattr(settings, 'allowed_private_hosts', '')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='Private'):
        checked_url('https://public-looking.example/api')
    with pytest.raises(ValueError):
        checked_url('https://username:password@example.com/api')
    with pytest.raises(ValueError):
        checked_url('https://example.com/api#https://127.0.0.1')
    monkeypatch.setattr(settings, 'allowed_private_hosts', 'public-looking.example')
    assert checked_url('https://public-looking.example/api')


def test_catalog_formats_are_fixed_and_cannot_be_redefined(login, db):
    seed_catalog(db)
    db.commit()
    client = login('root')
    rows = client.get('/api/categories').json()['items']
    assert len(rows) == 7
    assert sum(f['enabled'] for row in rows for f in row['formats']) == 13
    anthropic = next(row for row in rows if row['family'] == 'Anthropic')
    builtin = anthropic['formats'][0]
    assert client.patch('/api/formats/' + builtin['id'], json={'enabled': False}).status_code == 404
    assert client.post('/api/formats', json={'category_id': anthropic['id'], 'code': 'api_key-v1', 'version': '1',
        'name': 'Pretend implementation', 'schema_config': {'type': 'api_key', 'remote_type': 1}, 'enabled': True}).status_code == 405
    assert client.post('/api/formats', json={'category_id': anthropic['id'], 'code': 'custom-claude', 'version': '2',
        'name': 'Claude 自定义凭据', 'schema_config': {'type': 'api_key', 'remote_type': 14}, 'enabled': True}).status_code == 405


def test_announcements_versions_unread_and_sanitization(login):
    root, user = login('root'), login('user')
    created = root.post('/api/announcements', json={'title_zh': '公告', 'content_zh': '<p>内容</p><script>alert(1)</script>',
                                                  'status': 'published', 'audience': ['user']})
    assert created.status_code == 201
    item = created.json()
    assert item['content_zh'] == '内容'
    listing = user.get('/api/announcements').json()
    assert listing['unread_count'] == 1
    assert listing['items'][0]['content_en'] == '内容'
    assert user.post(f"/api/announcements/{item['id']}/read").status_code == 200
    assert user.get('/api/announcements').json()['unread_count'] == 0
    assert root.patch(f"/api/announcements/{item['id']}", json={'status': 'withdrawn'}).status_code == 200
    assert user.get('/api/announcements').json()['items'] == []
    assert root.patch(f"/api/announcements/{item['id']}", json={'status': 'published'}).status_code == 200
    assert user.get('/api/announcements').json()['unread_count'] == 1
    assert login('admin').get('/api/announcements').json()['items'] == []
    assert user.post('/api/announcements', json={'title_zh': 'No'}).status_code == 403


def test_site_credentials_hidden_from_non_superadmin(login, db, monkeypatch):
    from app.models import Site
    from app.routers import sites
    from test_site_distribution import add_enabled_template
    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    def fake_verify(row):
        from app.db import utcnow
        row.health, row.verified_at = 'healthy', utcnow()
        row.capabilities = {'read': 'supported', 'formats': ['api_key-v1'], 'create': 'supported', 'can_write': True,
                            'channel_config': 'supported', 'models': ['model-a'], 'groups': ['default']}
        return True
    monkeypatch.setattr(sites, 'verify_site', fake_verify)
    root = login('root')
    response = root.post('/api/sites', json={'name': 'Test site', 'prefix': 'test', 'base_url': 'https://site.example',
        'seller_user_id': '1', 'token': 'example-token-for-mocked-test', 'enabled': True})
    assert response.status_code == 201
    assert 'token' not in response.json() and 'token_encrypted' not in response.json()
    assert not response.json()['enabled']
    site = db.get(Site, response.json()['id'])
    add_enabled_template(db, site)
    db.commit()
    assert root.patch('/api/sites/' + site.id, json={'enabled': True}).json()['enabled']
    ordinary = login('user')
    item = ordinary.get('/api/sites').json()['items'][0]
    assert not {'base_url', 'token_hint', 'token', 'seller_user_id', 'stats_config'} & item.keys()
    assert ordinary.post(f"/api/sites/{item['id']}/verify").status_code == 403
    assert root.request('DELETE', f"/api/sites/{item['id']}", json={'confirmation': 'wrong'}).status_code == 422
    assert root.request('DELETE', f"/api/sites/{item['id']}", json={'confirmation': 'Test site'}).status_code == 200


@pytest.mark.parametrize(('bucket', 'limit', 'method', 'path', 'body'), [
    ('write', 120, 'PATCH', '/api/auth/profile', {'nickname': 'Limited write'}),
    ('reveal', 20, 'POST', '/api/channels/no-such-channel/reveal', None),
    ('download', 60, 'GET', '/api/usage/export', None),
])
def test_authenticated_operation_rate_buckets(login, users, bucket, limit, method, path, body):
    import time

    from app.auth import get_redis
    client = login('user')
    window = int(time.time()) // 60
    # Cover a minute rollover without relying on sleeps or real-time scheduling.
    for minute in (window, window + 1):
        get_redis().set(f"rate:{bucket}:{users['user'].id}:{minute}", limit, ex=90)
    response = client.request(method, path, json=body)
    assert response.status_code == 429
    assert 0 < int(response.headers['Retry-After']) <= 60
    assert client.get('/api/auth/me').status_code == 200


def test_write_rate_limit_shared_across_user_sessions(login, users):
    import time

    from app.auth import get_redis
    first, second = login('user'), login('user')
    window = int(time.time()) // 60
    for minute in (window, window + 1):
        get_redis().set(f"rate:write:{users['user'].id}:{minute}", 119, ex=90)
    assert first.patch('/api/auth/profile', json={'nickname': 'Allowed last write'}).status_code == 200
    assert second.patch('/api/auth/profile', json={'nickname': 'Over limit'}).status_code == 429
    assert login('sibling').patch('/api/auth/profile', json={'nickname': 'Independent account'}).status_code == 200


@pytest.mark.parametrize(('status', 'expected'), [
    (401, 'authentication_error'), (403, 'permission_denied'), (429, 'rate_limited'),
    (500, 'server_error'), (302, 'protocol_error'),
])
def test_site_verification_classifies_sanitized_http_failure(monkeypatch, status, expected):
    import httpx
    from app.adapters import silicon
    from app.models import Site
    from app.routers.sites import site_json, verify_site
    secret = 'fixture-seller-token-never-return-this'
    monkeypatch.setattr(silicon, 'safe_request', lambda *args, **kwargs:
                        httpx.Response(status, json={'success': False, 'message': secret}))
    site = Site(name='Fixture', prefix='ERROR', base_url='https://invalid.example', seller_user_id='1',
                token_encrypted=encrypt(secret), token_hint='masked', enabled=True)
    assert verify_site(site) is False
    assert site.health == expected and site.enabled is False
    assert site_json(site)['verification_error']['category'] == expected
    assert secret not in str(site_json(site))


def test_site_verification_distinguishes_network_and_identity_failures(monkeypatch):
    import httpx
    from app.adapters import silicon
    from app.models import Site
    from app.routers.sites import verify_site
    site = Site(name='Fixture', prefix='ERR2', base_url='https://invalid.example', seller_user_id='1',
                token_encrypted=encrypt('fixture-token'), token_hint='masked', enabled=True)
    def broken_network(*args, **kwargs):
        raise httpx.ConnectError('Sensitive upstream details must not be exposed')
    monkeypatch.setattr(silicon, 'safe_request', broken_network)
    assert verify_site(site) is False and site.health == 'connection_error'
    monkeypatch.setattr(silicon, 'safe_request', lambda *args, **kwargs:
                        httpx.Response(200, json={'success': True, 'data': {'id': 2}}))
    assert verify_site(site) is False and site.health == 'identity_mismatch'
