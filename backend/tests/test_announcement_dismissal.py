"""Daily dismissal is account/version scoped and follows the Beijing calendar."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db import SessionLocal, utcnow
from app.models import Announcement, AnnouncementRead, User
from app.routers import announcements as routes
from sqlalchemy import func, inspect, select, text


@pytest.fixture
def announcement(db, users):
    row = Announcement(title_zh='每日公告', content_zh='固定测试内容', status='published',
        audience=['superadmin', 'admin', 'user'], publisher_id=users['root'].id, published_at=utcnow())
    db.add(row)
    db.commit()
    return row


def utc_time(day, hour, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC).replace(tzinfo=None)


def item(client, announcement):
    listing = client.get('/api/announcements')
    assert listing.status_code == 200, listing.text
    assert listing.json()['timezone'] == 'Asia/Shanghai'
    return next(row for row in listing.json()['items'] if row['id'] == announcement.id)


@pytest.mark.parametrize('role', ['root', 'admin', 'user'])
def test_all_roles_can_read_and_dismiss_without_conflating_the_two_states(login, db, announcement, role, monkeypatch):
    monkeypatch.setattr(routes, 'utcnow', lambda: utc_time(9, 8))
    client = login(role)
    assert item(client, announcement)['unread'] is True
    assert item(client, announcement)['dismissed_today'] is False
    response = client.post(f'/api/announcements/{announcement.id}/dismiss-today', json={'version': announcement.version})
    assert response.status_code == 200 and response.json() == {'ok': True, 'version': 1, 'dismissed_on': '2026-09-09'}
    assert item(client, announcement)['unread'] is True
    assert item(client, announcement)['dismissed_today'] is True
    assert db.scalar(select(AnnouncementRead)).read_at is None
    assert client.post(f'/api/announcements/{announcement.id}/read').status_code == 200
    assert item(client, announcement)['unread'] is False and item(client, announcement)['dismissed_today'] is True
    db.expire_all()
    original_read_at = db.scalar(select(AnnouncementRead)).read_at
    assert client.post(f'/api/announcements/{announcement.id}/read').status_code == 200
    assert client.post(f'/api/announcements/{announcement.id}/dismiss-today', json={'version': 1}).status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 1
    assert db.scalar(select(AnnouncementRead)).read_at == original_read_at


def test_read_only_announcements_remain_eligible_every_day_until_explicit_daily_dismissal(login, db, announcement, monkeypatch):
    current = [utc_time(9, 15, 59, 59)]
    monkeypatch.setattr(routes, 'utcnow', lambda: current[0])
    client = login('admin')
    path = f'/api/announcements/{announcement.id}'
    assert client.post(path + '/read').status_code == 200
    for _ in range(2):
        assert item(client, announcement)['unread'] is False
        assert item(client, announcement)['dismissed_today'] is False
    assert client.post(path + '/dismiss-today', json={'version': 1}).json()['dismissed_on'] == '2026-09-09'
    assert item(client, announcement)['dismissed_today'] is True
    current[0] = utc_time(9, 16)
    listing = client.get('/api/announcements').json()
    assert listing['today'] == '2026-09-10'
    assert listing['items'][0]['dismissed_today'] is False and listing['items'][0]['unread'] is False
    db.expire_all()
    assert db.scalar(select(AnnouncementRead)).dismissed_on == date(2026, 9, 9), 'GET never clears or rewrites history'
    assert client.post(path + '/dismiss-today', json={'version': 1}).json()['dismissed_on'] == '2026-09-10'
    assert item(client, announcement)['dismissed_today'] is True


def test_daily_dismissal_is_account_scoped_and_survives_new_sessions(login, announcement):
    first = login('admin')
    assert first.post(f'/api/announcements/{announcement.id}/dismiss-today', json={'version': 1}).status_code == 200
    assert item(login('admin'), announcement)['dismissed_today'] is True
    assert item(login('other_admin'), announcement)['dismissed_today'] is False
    assert item(login('user'), announcement)['dismissed_today'] is False


def test_new_version_is_unread_and_not_dismissed_and_stale_dialog_cannot_close_it(login, db, announcement):
    client, root = login('admin'), login('root')
    path = f'/api/announcements/{announcement.id}'
    assert client.post(path + '/read').status_code == 200
    assert client.post(path + '/dismiss-today', json={'version': 1}).status_code == 200
    changed = root.patch(path, json={'content_zh': '更新后的内容'})
    assert changed.status_code == 200 and changed.json()['version'] == 2
    response = client.post(path + '/dismiss-today', json={'version': 1})
    assert response.status_code == 409 and response.json()['detail'] == '公告已更新，请刷新后查看最新内容'
    value = item(client, announcement)
    assert value['unread'] and not value['dismissed_today'] and value['version'] == 2
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 1
    assert client.post(path + '/dismiss-today', json={'version': 2}).status_code == 200
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 2


@pytest.mark.parametrize(('status', 'audience'), [('draft', ['admin']), ('withdrawn', ['admin']), ('published', ['user'])])
def test_admin_cannot_read_or_dismiss_drafts_withdrawn_or_other_audiences(login, db, announcement, status, audience):
    announcement.status, announcement.audience = status, audience
    db.commit()
    client = login('admin')
    assert client.get('/api/announcements').json()['items'] == []
    assert client.post(f'/api/announcements/{announcement.id}/read').status_code == 404
    assert client.post(f'/api/announcements/{announcement.id}/dismiss-today', json={'version': 1}).status_code == 404
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 0


@pytest.mark.parametrize('body', [{}, {'version': True}, {'version': '1'}, {'version': 0}, {'version': 1, 'user_id': 'other'}])
def test_dismissal_requires_explicit_valid_version_and_no_spoofed_account(login, db, announcement, body):
    response = login('user').post(f'/api/announcements/{announcement.id}/dismiss-today', json=body)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 0


def test_admin_cannot_publish_and_dismissal_still_requires_csrf(login, db, announcement):
    client = login('admin')
    assert client.post('/api/announcements', json={'title_zh': 'No'}).status_code == 403
    assert client.patch(f'/api/announcements/{announcement.id}', json={'title_zh': 'No'}).status_code == 403
    client.headers.pop('X-CSRF-Token')
    assert client.post(f'/api/announcements/{announcement.id}/dismiss-today', json={'version': 1}).status_code == 403
    assert db.scalar(select(func.count()).select_from(AnnouncementRead)) == 0


def test_concurrent_read_and_dismiss_are_idempotent_and_preserve_both_states(db, users, announcement):
    announcement_id, user_id = announcement.id, users['admin'].id
    barrier = Barrier(6)
    def act(index):
        with SessionLocal() as session:
            user = session.get(User, user_id)
            barrier.wait(timeout=10)
            if index % 2:
                return routes.read_announcement(announcement_id, user=user, db=session)
            return routes.dismiss_announcement_today(announcement_id, routes.AnnouncementDismiss(version=1), user=user, db=session)
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(act, range(6)))
    assert all(outcome['ok'] for outcome in outcomes)
    db.expire_all()
    records = list(db.scalars(select(AnnouncementRead)))
    assert len(records) == 1 and records[0].read_at is not None
    assert records[0].dismissed_on == routes.beijing_today()


def test_migration_preserves_existing_read_records_and_makes_dismissal_independent(db, users, announcement):
    path = Path(__file__).parents[1] / 'migrations' / 'versions' / 'e508cb6a721f_announcement_daily_dismissal.py'
    spec = importlib.util.spec_from_file_location('announcement_dismissal_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(db.connection()))
    assert module.down_revision == 'd204ef83a951'
    module.downgrade()
    db.execute(text('INSERT INTO announcement_reads (id,user_id,announcement_id,version,read_at) '
        'VALUES (:id,:user,:announcement,1,:read_at)'), {'id': 'legacy-read', 'user': users['user'].id,
        'announcement': announcement.id, 'read_at': utc_time(8, 10)})
    before = dict(db.execute(text('SELECT * FROM announcement_reads')).mappings().one())
    module.upgrade()
    after = dict(db.execute(text('SELECT * FROM announcement_reads')).mappings().one())
    assert after == {**before, 'dismissed_on': None}
    columns = {column['name']: column for column in inspect(db.connection()).get_columns('announcement_reads')}
    assert columns['read_at']['nullable'] and columns['dismissed_on']['nullable']
    db.add(AnnouncementRead(user_id=users['admin'].id, announcement_id=announcement.id, version=1, dismissed_on=date(2026, 9, 9)))
    db.commit()
    new = db.scalar(select(AnnouncementRead).where(AnnouncementRead.user_id == users['admin'].id))
    assert new.read_at is None
