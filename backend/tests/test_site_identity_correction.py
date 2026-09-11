"""An unsuccessful initial read cannot permanently bind a mistyped account ID."""
from types import SimpleNamespace

import pytest
from app.models import Site
from app.models_channels import Task, TaskItem
from app.routers import sites
from app.security import encrypt


@pytest.mark.parametrize(('operation', 'status', 'stage', 'attempted', 'allowed'), [
    ('sync', 'failed', 'queued', False, True),
    ('sync', 'pending', 'queued', False, False),
    ('sync', 'succeeded', 'queued', False, False),
    ('sync', 'failed', 'queued', True, False),
    ('sync', 'failed', 'reconcile', False, False),
    ('create', 'failed', 'queued', False, False),
])
def test_correct_account_only_after_failed_initial_reads(
        db, users, login, monkeypatch, operation, status, stage, attempted, allowed):
    site = Site(name='Fixture', prefix='IDENTITY', base_url='https://fixture.invalid',
                seller_user_id='31', adapter='silicon-v1', token_encrypted=encrypt('fixture-token'),
                enabled=False)
    task = Task(actor_id=users['root'].id, owner_id=users['root'].id, kind='sync',
                actor_session_version=users['root'].session_version)
    db.add_all([site, task])
    db.flush()
    item = TaskItem(task_id=task.id, site_id=site.id, operation=operation, status=status,
                    stage=stage, remote_write_attempted=attempted)
    db.add(item)
    db.commit()
    checked = []

    def adapter(row):
        checked.append(row.seller_user_id)
        return SimpleNamespace(verify=lambda: {'capabilities': {}, 'verified_version': ''})

    monkeypatch.setattr(sites, 'get_adapter', adapter)
    result = login('root').patch(f'/api/sites/{site.id}', json={'seller_user_id': '49'})
    assert result.status_code == (200 if allowed else 409)
    db.refresh(site)
    assert site.seller_user_id == ('49' if allowed else '31')
    assert checked == (['49'] if allowed else [])
    db.refresh(item)
    assert item.remote_write_attempted is attempted
    assert item.status == status
