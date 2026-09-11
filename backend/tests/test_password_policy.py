import pytest
from app.models import User
from app.security import hash_password, verify_password


@pytest.mark.parametrize('password', ['aB3!?x', '密钥🔐密码好', 'x' * 256])
def test_create_account_accepts_password_boundaries_and_keeps_hash(login, users, db, password):
    root = login('root')
    response = root.post('/api/users', json={
        'username': 'password_boundary', 'nickname': '边界账号', 'password': password,
        'role': 'user',
    })
    assert response.status_code == 201
    account = db.get(User, response.json()['id'])
    assert account.password_hash != password
    assert account.password_hash.startswith('$argon2id$')
    assert verify_password(password, account.password_hash)
    assert root.post('/api/auth/login', json={
        'username': account.username, 'password': password,
    }).status_code == 200


@pytest.mark.parametrize('action', ['create', 'reset', 'profile'])
@pytest.mark.parametrize(('password', 'message'), [
    ('k9!pZ', '至少需要 6 位'),
    ('密钥🔐密码', '至少需要 6 位'),
    ('private-value-' * 20, '最多允许 256 位'),
    ({'secret': 'private-invalid-password'}, '必须是文本'),
])
def test_invalid_passwords_are_chinese_redacted_and_leave_existing_password_unchanged(
    login, users, db, action, password, message,
):
    child = login('user')
    before_hash, before_version = users['user'].password_hash, users['user'].session_version
    if action == 'create':
        response = login('root').post('/api/users', json={
            'username': 'invalid_password', 'nickname': 'Invalid', 'password': password,
            'role': 'user',
        })
    elif action == 'reset':
        response = login('admin').patch('/api/users/' + users['user'].id, json={'password': password})
    else:
        response = child.patch('/api/auth/profile', json={
            'current_password': 'test-password-123!', 'password': password,
        })
    assert response.status_code == 422
    errors = response.json()['detail']
    assert errors and all(set(error) == {'loc', 'msg', 'type'} for error in errors)
    assert next(error for error in errors if error['loc'][-1] == 'password')['msg'] == message
    for secret in ([password] if isinstance(password, str) else list(password.values())):
        assert secret not in response.text
    assert 'test-password-123!' not in response.text
    db.refresh(users['user'])
    assert (users['user'].password_hash, users['user'].session_version) == (before_hash, before_version)
    assert child.get('/api/auth/me').status_code == 200


@pytest.mark.parametrize('password', ['', None])
def test_admin_reset_empty_or_null_password_preserves_account_and_sessions(login, users, db, password):
    child, admin = login('user'), login('admin')
    before_hash, before_version = users['user'].password_hash, users['user'].session_version
    response = admin.patch('/api/users/' + users['user'].id, json={
        'password': password, 'nickname': 'Updated nickname',
    })
    assert response.status_code == 200
    db.refresh(users['user'])
    assert users['user'].nickname == 'Updated nickname'
    assert (users['user'].password_hash, users['user'].session_version) == (before_hash, before_version)
    assert child.get('/api/auth/me').status_code == 200


def test_login_keeps_short_password_compatibility_and_returns_chinese_error(client, users, db):
    # Existing short passwords remain verifiable; only setting a new password applies the minimum.
    from argon2 import PasswordHasher
    users['user'].password_hash = PasswordHasher().hash('a')
    db.commit()
    response = client.post('/api/auth/login', json={'username': 'user', 'password': 'a'})
    assert response.status_code == 200
    failed = client.post('/api/auth/login', json={'username': 'user', 'password': 'b'})
    assert failed.status_code == 401
    assert failed.json()['detail'] == '用户名或密码错误'


def test_profile_requires_correct_current_password_before_six_character_change(login, users, db):
    client = login('user')
    before_hash, before_version = users['user'].password_hash, users['user'].session_version
    response = client.patch('/api/auth/profile', json={
        'current_password': 'private-wrong-password', 'password': 'abc123',
    })
    assert response.status_code == 400
    assert response.json()['detail'] == '当前密码错误'
    assert 'private-wrong-password' not in response.text
    db.refresh(users['user'])
    assert (users['user'].password_hash, users['user'].session_version) == (before_hash, before_version)
    assert client.get('/api/auth/me').status_code == 200


def test_current_password_length_error_is_localized_and_redacted(login):
    response = login('user').patch('/api/auth/profile', json={
        'current_password': 'private-current-password-' * 12, 'password': 'abc123',
    })
    assert response.status_code == 422
    assert response.json()['detail'] == [{
        'loc': ['body', 'current_password'], 'msg': '最多允许 256 位', 'type': 'string_too_long',
    }]


@pytest.mark.parametrize(('password', 'message'), [
    ('12345', '至少需要 6 位'), ('x' * 257, '最多允许 256 位'),
])
def test_direct_password_hashing_enforces_same_policy(password, message):
    with pytest.raises(ValueError, match=message):
        hash_password(password)
