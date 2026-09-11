"""Local HTTP contract tests: no real seller, credentials, or production Redis.

The only substitute is the lock service. HTTP requests go through the production
DNS validation, pinned connection, JSON decoder, adapter, and durable worker.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from app import worker
from app.adapters import silicon
from app.adapters.silicon import SiliconAdapter
from app.config import settings
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_channels import Distribution, TaskItem
from app.network import safe_request
from app.security import encrypt


@pytest.fixture
def local_seller(monkeypatch):
    state = SimpleNamespace(records={}, creates=[], requests=[], delay_create=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No HTTP server request/header logging in tests.

        def respond(self, data=None, *, status=200, success=True):
            encoded = json.dumps({'success': success, 'data': data}).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Expected when exercising accepted-write response timeout.

        def authorized(self):
            state.requests.append((self.command, self.path, self.headers.get('New-Api-User')))
            return (self.headers.get('Authorization') == 'Bearer test-only-http-seller-token'
                    and self.headers.get('New-Api-User') == '123')

        def do_GET(self):
            if not self.authorized():
                self.respond(status=401, success=False)
                return
            parsed = urlsplit(self.path)
            if parsed.path == '/api/status':
                self.respond({'version': SiliconAdapter.VERSION})
            elif parsed.path == '/api/user/self':
                self.respond({'id': 123})
            elif parsed.path == '/api/seller/channel/meta':
                self.respond({'models': [{'id': 'test-model'}], 'groups': ['default']})
            elif parsed.path == '/api/seller/channel/':
                keyword = parse_qs(parsed.query).get('keyword', [''])[0]
                rows = [{k: v for k, v in row.items() if k != 'key'} for row in state.records.values()
                        if keyword in row['name']]
                self.respond({'items': rows, 'total': len(rows), 'page': 1, 'page_size': 100,
                              'can_write': True, 'can_toggle': True, 'can_edit_routing': False})
            elif parsed.path.startswith('/api/seller/channel/'):
                remote_id = parsed.path.rstrip('/').split('/')[-1]
                row = state.records.get(remote_id)
                self.respond({k: v for k, v in row.items() if k != 'key'} if row else None,
                             status=200 if row else 404, success=bool(row))
            else:
                self.respond(status=404, success=False)

        def do_POST(self):
            if not self.authorized():
                self.respond(status=401, success=False)
                return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            if self.path != '/api/seller/channel/':
                self.respond(status=404, success=False)
                return
            valid = (body.get('mode') == 'single' and isinstance(body.get('channel'), dict)
                     and isinstance(body['channel'].get('settings'), dict)
                     and isinstance(body['channel'].get('proxy'), str)
                     and not {'priority', 'weight', 'setting', 'auto_ban', 'status_code_mapping',
                              'param_override', 'header_override', 'tag'} & body['channel'].keys())
            if not valid:
                self.respond(success=False)
                return
            remote_id = str(1001 + len(state.creates))
            state.creates.append(body)
            state.records[remote_id] = {**body['channel'], 'id': int(remote_id),
                                        'settings': json.dumps(body['channel']['settings'])}
            # Commit the remote side effect BEFORE delaying its acknowledgement.
            if state.delay_create:
                time.sleep(state.delay_create)
            self.respond()  # Real documented shape: success=true, no data.id.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    monkeypatch.setattr(settings, 'allow_http_sites', True)
    monkeypatch.setattr(settings, 'allowed_private_hosts', '127.0.0.1')
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LocalLockClient:
    """Lease API double; does not replace any HTTP or durable queue operations."""
    def __init__(self):
        self.locks = {}

    def lock(self, name, **_):
        mutex = self.locks.setdefault(name, threading.Lock())

        class Lock:
            acquired = False

            def acquire(self, blocking=False):
                self.acquired = mutex.acquire(blocking=blocking)
                return self.acquired

            def owned(self):
                return self.acquired

            def extend(self, *_, **__):
                return self.acquired

            def release(self):
                if self.acquired:
                    self.acquired = False
                    mutex.release()

        return Lock()


@pytest.fixture
def pending_http_upload(db, users, login, local_seller, monkeypatch):
    base_url, state = local_seller
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: None)
    category = Category(id=uid(), name='OpenAI', family='OpenAI')
    db.add(category)
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=category.id, name='API Key v1', code='api_key-v1',
                           version='1', enabled=True, schema_config={'type': 'api_key', 'remote_type': 1}, default_models=['test-model'])
    site = Site(id=uid(), name='Local contract fixture', prefix='HTTP', base_url=base_url,
                seller_user_id='123', token_encrypted=encrypt('test-only-http-seller-token'), enabled=True)
    verified = SiliconAdapter(site).verify()
    assert verified['identity'] == {'id': 123}
    site.capabilities = {**verified['capabilities'], 'verified_version': verified['verified_version']}
    site.verified_at = utcnow()
    db.add_all([fmt, site])
    db.commit()
    client = login('user')
    result = client.post('/api/uploads/submit', json={'category_id': category.id, 'format_id': fmt.id,
                         'credentials': 'upstream-placeholder-key-for-http-test', 'models': ['test-model'],
                         'idempotency_key': 'local-http-upload-unique-id'})
    assert result.status_code == 200, result.text
    return result.json(), client, state


def test_worker_real_http_create_wrapper_identity_and_readback(db, pending_http_upload):
    task, client, state = pending_http_upload
    assert worker.run_once(LocalLockClient())
    db.expire_all()
    item = db.get(TaskItem, task['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    assert item.status == 'succeeded' and dist.status == 'disabled'
    assert dist.remote_id == '1001' and dist.key_version == 1
    assert len(state.creates) == 1
    channel = state.creates[0]['channel']
    assert channel['status'] == 2 and channel['models'] == 'test-model'
    assert channel['settings'] == {} and channel['proxy'] == '' and 'setting' not in channel
    assert channel['key'] == 'upstream-placeholder-key-for-http-test'
    assert all(user_id == '123' for _, _, user_id in state.requests)
    assert any(path.startswith('/api/seller/channel/?') and 'keyword=' in path for _, path, _ in state.requests)
    listing = client.get('/api/channels').text
    assert 'upstream-placeholder-key-for-http-test' not in listing
    assert 'test-only-http-seller-token' not in listing


def test_worker_real_http_accepted_create_timeout_recovered_once(db, pending_http_upload, monkeypatch):
    task, client, state = pending_http_upload
    state.delay_create = 0.2

    def bounded_request(method, url, *, headers=None, json=None, params=None, timeout=20):
        # Shorten only this test's create-response wait; all network logic stays real.
        return safe_request(method, url, headers=headers, json=json, params=params,
                            timeout=0.05 if method == 'POST' else timeout)

    monkeypatch.setattr(silicon, 'safe_request', bounded_request)
    locks = LocalLockClient()
    assert worker.run_once(locks)
    db.expire_all()
    item = db.get(TaskItem, task['items'][0]['id'])
    assert item.status == 'needs_review' and item.stage == 'create_sent'
    assert len(state.creates) == 1
    assert client.post('/api/tasks/' + task['id'] + '/retry').status_code == 409
    assert client.post('/api/tasks/' + task['id'] + '/reconcile').status_code == 200
    db.commit()
    assert worker.run_once(locks)
    db.expire_all()
    item = db.get(TaskItem, item.id)
    dist = db.get(Distribution, item.distribution_id)
    assert item.status == 'succeeded' and dist.remote_id == '1001'
    assert len(state.creates) == 1  # No second remote create after the timed-out response.
