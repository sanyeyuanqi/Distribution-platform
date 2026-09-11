import pytest
from app.models import Site
from app.models_channels import Task, TaskItem
from app.security import encrypt
from sqlalchemy import func, select


@pytest.mark.parametrize('role', ['root', 'admin', 'user', 'other_admin'])
@pytest.mark.parametrize('scoped', [False, True])
def test_manual_sync_is_unavailable_even_with_collectible_sites(db, users, login, monkeypatch, role, scoped):
    notified = []
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: notified.append(True))
    db.add(Site(name='Scope test', prefix='scope', base_url='https://scope.invalid', seller_user_id='1',
        token_encrypted=encrypt('test-only'), collect_enabled=True))
    db.commit()
    payload = {'owner_id': users['admin'].id} if scoped else {}
    assert login(role).post('/api/sync', json=payload).status_code == 404
    assert db.scalar(select(func.count()).select_from(Task)) == 0
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 0
    assert not notified
