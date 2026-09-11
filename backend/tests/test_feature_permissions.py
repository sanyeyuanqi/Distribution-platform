"""Feature permissions are enforced independently of record ownership."""
import json

import pytest
from app import worker
from app.db import uid, utcnow
from app.models import (
    Announcement,
    AnnouncementRead,
    Category,
    CredentialFormat,
    Site,
    SiteUploadTemplate,
)
from app.models_channels import Distribution, Task, TaskItem
from app.security import encrypt
from sqlalchemy import func, select


@pytest.fixture
def feature_data(db, users, monkeypatch):
    for module in ('uploads', 'channels', 'tasks'):
        monkeypatch.setattr(f'app.routers.{module}.notify_worker', lambda: None)

    def no_network(*args, **kwargs):
        pytest.fail('Permission tests must not contact an upstream site')

    monkeypatch.setattr('app.adapters.silicon.safe_request', no_network)
    category = Category(id=uid(), name='OpenAI', family='OpenAI')
    site = Site(id=uid(), name='Permission site', prefix='permissions', base_url='https://permissions.invalid',
        seller_user_id='1', token_encrypted=encrypt('permission-fixture-token'), enabled=True,
        adapter='silicon-v1', health='healthy', verified_at=utcnow(), capabilities={
            'can_write': True, 'create': 'supported', 'formats': ['api_key-v1'],
            'models': ['permission-model'], 'groups': ['default']})
    db.add_all([category, site])
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=category.id, code='api_key-v1', name='API Key', version='1',
        enabled=True, schema_config={'type': 'api_key', 'remote_type': 1})
    db.add(fmt)
    db.flush()
    db.add(SiteUploadTemplate(site_id=site.id, category_id=category.id, format_id=fmt.id,
        models=['permission-model'], routing_group='default', enabled=True))
    announcement = Announcement(id=uid(), title_zh='Public fixture', content_zh='Published for every role',
        status='published', audience=['superadmin', 'admin', 'user'], publisher_id=users['root'].id,
        published_at=utcnow())
    db.add(announcement)
    tasks = {}
    for role in ('admin', 'user', 'other_user'):
        actor = users[role]
        task = Task(id=uid(), actor_id=actor.id, owner_id=actor.id, actor_session_version=actor.session_version,
            kind='sync', status='queued', snapshot={})
        db.add(task)
        db.flush()
        db.add(TaskItem(task_id=task.id, site_id=site.id, operation='sync', status='pending'))
        tasks[role] = task
    db.commit()
    return {'site': site, 'category': category, 'announcement': announcement, 'tasks': tasks}


@pytest.mark.parametrize('role', ['admin', 'other_admin'])
def test_admin_feature_denials_apply_even_to_owned_records_and_direct_urls(db, login, feature_data, role):
    client = login(role)
    announcement_id = feature_data['announcement'].id
    requests = [
        ('POST', '/api/announcements', {'title_zh': 'Unauthorized announcement'}),
        ('PATCH', f'/api/announcements/{announcement_id}', {'title_zh': 'Unauthorized edit'}),
        ('GET', '/api/audit', None),
        ('GET', '/api/audit?action=auth.login', None),
        ('GET', '/api/tasks', None),
        ('GET', '/api/tasks?scope=mine', None),
    ]
    for task_id in [feature_data['tasks']['admin'].id, feature_data['tasks']['user'].id, 'missing-task']:
        requests.append(('GET', '/api/tasks/' + task_id, None))
        requests.extend(('POST', f'/api/tasks/{task_id}/{action}', None)
                        for action in ('retry', 'reconcile', 'reprepare-templates', 'cancel'))
    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
    db.expire_all()
    assert all(not task.cancelled and task.status == 'queued' for task in feature_data['tasks'].values())
    assert feature_data['announcement'].title_zh == 'Public fixture'
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 0
    listing = client.get('/api/announcements')
    assert listing.status_code == 200 and listing.json()['items'][0]['id'] == announcement_id
    assert client.post(f'/api/announcements/{announcement_id}/read').status_code == 200
    assert client.post(f'/api/announcements/{announcement_id}/dismiss-today', json={'version': 1}).status_code == 200


@pytest.mark.parametrize('role', ['root', 'user'])
def test_superadmin_and_user_retain_announcements_and_scoped_task_access(db, login, feature_data, role):
    client = login(role)
    announcement_id = feature_data['announcement'].id
    listing = client.get('/api/announcements')
    assert listing.status_code == 200 and listing.json()['items'][0]['id'] == announcement_id
    assert client.post(f'/api/announcements/{announcement_id}/read').status_code == 200
    own_task = feature_data['tasks']['user']
    tasks = client.get('/api/tasks')
    assert tasks.status_code == 200
    assert own_task.id in {task['id'] for task in tasks.json()['items']}
    assert client.get('/api/tasks/' + own_task.id).status_code == 200
    assert client.post('/api/tasks/' + own_task.id + '/cancel').status_code == 200
    db.refresh(own_task)
    assert own_task.cancelled
    foreign_status = 200 if role == 'root' else 404
    assert client.get('/api/tasks/' + feature_data['tasks']['other_user'].id).status_code == foreign_status
    assert client.get('/api/audit').status_code == (200 if role == 'root' else 403)


def test_dashboard_does_not_expose_admin_task_data_through_scope_variants(login, users, feature_data):
    client = login('admin')
    paths = ['/api/dashboard', '/api/dashboard?scope=mine',
             '/api/dashboard?owner_id=' + users['user'].id, '/api/dashboard?owner_id=' + users['admin'].id]
    task_ids = [task.id for task in feature_data['tasks'].values()]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200 and response.json()['recent_tasks'] == []
        assert all(task_id not in response.text for task_id in task_ids)
    own = login('user').get('/api/dashboard').json()['recent_tasks']
    assert {task['id'] for task in own} == {feature_data['tasks']['user'].id}
    all_tasks = login('root').get('/api/dashboard').json()['recent_tasks']
    assert {task['id'] for task in all_tasks} == set(task_ids)


def test_admin_upload_and_sync_still_submit_and_worker_executes_authorized_upload(db, login, feature_data, monkeypatch):
    client = login('admin')
    payload = {'category_id': feature_data['category'].id, 'credentials': 'permission-only-placeholder-key'}
    assert client.get('/api/uploads/options').status_code == 200
    preview = client.post('/api/uploads/simple-preview', json=payload)
    assert preview.status_code == 200 and preview.json()['can_submit']
    uploaded = client.post('/api/uploads/simple-submit', json={**payload, 'idempotency_key': 'permission-upload-001'})
    assert uploaded.status_code == 200, uploaded.text
    task = db.get(Task, uploaded.json()['id'])
    assert task.owner_id == task.actor_id and task.status == 'queued'
    assert client.get('/api/tasks/' + task.id).status_code == 403
    sent = {}

    class Adapter:
        def __init__(self, site, before_write):
            self.before_write = before_write

        def find_unique_name(self, name):
            return None

        def create(self, **values):
            self.before_write()
            sent.update(values)
            return '501'

        def detail(self, remote_id):
            return {'id': 501, 'name': sent['name'], 'type': 1, 'status': 2,
                    'models': ','.join(sent['models']), 'group': sent['group']}

    monkeypatch.setattr(worker, 'get_adapter', Adapter)
    item = db.get(TaskItem, uploaded.json()['items'][0]['id'])
    worker.execute_item(db, item)
    distribution = db.get(Distribution, item.distribution_id)
    assert distribution.remote_id == '501' and distribution.status == 'disabled'
    assert sent['key'] == payload['credentials']
    task_count = db.scalar(select(func.count()).select_from(Task))
    item_count = db.scalar(select(func.count()).select_from(TaskItem))
    assert client.post('/api/sync', json={}).status_code == 404
    assert db.scalar(select(func.count()).select_from(Task)) == task_count
    assert db.scalar(select(func.count()).select_from(TaskItem)) == item_count
    assert payload['credentials'] not in json.dumps(uploaded.json())
