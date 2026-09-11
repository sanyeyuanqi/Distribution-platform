import os
import secrets
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

os.environ['DATABASE_URL'] = os.environ.get('TEST_DATABASE_URL', 'sqlite:///' + str(Path(tempfile.gettempdir()) / f'keyacross-test-{os.getpid()}.db'))
os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['SECRET_KEY'] = secrets.token_urlsafe(48)
os.environ['COOKIE_SECURE'] = 'false'
os.environ['ENVIRONMENT'] = 'test'
os.environ['BOOTSTRAP_USERNAME'] = ''
os.environ['BOOTSTRAP_PASSWORD'] = ''

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import Base, SessionLocal, engine, uid
from app.main import app
from app.models import User
from app.security import hash_password


@pytest.fixture
def db(monkeypatch, tmp_path):
    # TEST_DATABASE_URL must name a disposable database; guard against production wipes.
    if os.environ.get('TEST_DATABASE_URL') and 'test' not in engine.url.database.lower():
        raise RuntimeError('Use a dedicated database with test in its name')
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(auth, 'get_redis', lambda: redis)
    from app.config import settings
    monkeypatch.setattr(settings, 'attachment_dir', str(tmp_path / 'attachments'))
    with SessionLocal() as session:
        yield session
    Base.metadata.drop_all(engine)


@pytest.fixture
def users(db):
    hashed = hash_password('test-password-123!')
    result = {}
    for key, role, parent in [('root', 'superadmin', None), ('admin', 'admin', None), ('other_admin', 'admin', None),
                               ('user', 'user', 'admin'), ('sibling', 'user', 'admin'), ('other_user', 'user', 'other_admin')]:
        row = User(id=uid(), username=key, nickname=key.title(), password_hash=hashed, role=role,
            parent_id=result[parent].id if parent else None)
        db.add(row)
        db.flush()
        result[key] = row
    db.commit()
    return result


@pytest.fixture
def client(db):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def login(db, users):
    clients = []
    def factory(key='root'):
        client = TestClient(app)
        response = client.post('/api/auth/login', json={'username': users[key].username, 'password': 'test-password-123!'})
        assert response.status_code == 200, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        clients.append(client)
        return client
    yield factory
    for client in clients:
        client.close()
