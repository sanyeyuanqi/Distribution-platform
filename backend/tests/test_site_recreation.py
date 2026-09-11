"""Archival releases a site's public identity without reusing its historical identity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from app.bootstrap import seed_catalog
from app.models import AuditEvent, Category, Site, SiteUploadTemplate
from app.models_channels import (
    Channel,
    ChannelCredential,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UnclaimedChannel,
    UploadGroup,
)
from app.routers import sites
from app.security import decrypt, encrypt
from sqlalchemy import select

HISTORY_MODELS = (UploadGroup, Channel, ChannelCredential, KeyVersion, Distribution,
                  DistributionVersion, Task, TaskItem, UnclaimedChannel)


@pytest.fixture(autouse=True)
def mock_site_api(monkeypatch):
    state = SimpleNamespace(barrier=None)

    def verify():
        if state.barrier is not None:
            state.barrier.wait(timeout=10)
        return {'verified_version': 'v0.13.2', 'capabilities': {
            'read': 'supported', 'create': 'supported', 'can_write': True}}

    def forbid_network(*args, **kwargs):
        pytest.fail('Site recreation must not make an unstubbed remote request')

    monkeypatch.setattr(sites, 'normalize_url', lambda value: value.rstrip('/'))
    monkeypatch.setattr(sites, 'get_adapter', lambda row: SimpleNamespace(verify=verify))
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbid_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbid_network)
    return state


@pytest.fixture
def catalog(db):
    seed_catalog(db)
    db.commit()


def site_body(label='reusable', **values):
    return {'name': label, 'prefix': label, 'base_url': f'https://{label}.invalid',
            'seller_user_id': '71', 'token': f'{label}-token', **values}


def create(client, label='reusable', **values):
    response = client.post('/api/sites', json=site_body(label, **values))
    assert response.status_code == 201, response.text
    return response.json()


def archive(client, site):
    response = client.request('DELETE', '/api/sites/' + site['id'],
                              json={'confirmation': site['name']})
    assert response.status_code == 200, response.text
    assert response.json()['archived'] is True and response.json()['enabled'] is False


def row_snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def table_snapshot(db, *models):
    db.expire_all()
    return {model.__tablename__: [row_snapshot(row) for row in db.scalars(select(model).order_by(model.id))]
            for model in models}


def templates(db, site_id):
    db.expire_all()
    return list(db.scalars(select(SiteUploadTemplate).where(SiteUploadTemplate.site_id == site_id)
                           .order_by(SiteUploadTemplate.id)))


def add_history(db, site, template, owner):
    """Keep real foreign-key references and frozen credentials behind the archived site."""
    group = UploadGroup(owner_id=owner.id, category_id=template.category_id,
                        format_id=template.format_id, tag='original-upload', name='Original upload')
    db.add(group)
    db.flush()
    channel = Channel(owner_id=owner.id, group_id=group.id, category_id=template.category_id,
                      format_id=template.format_id, key_encrypted=encrypt('original-channel-key'),
                      key_hint='original-key', fingerprint='original-fingerprint', models=['original-model'])
    task = Task(actor_id=owner.id, owner_id=owner.id, actor_session_version=owner.session_version,
                kind='create', status='succeeded', group_id=group.id,
                snapshot={'site_ids': [site['id']], 'prefix': site['prefix']})
    db.add_all([channel, task])
    db.flush()
    distribution = Distribution(channel_id=channel.id, site_id=site['id'], remote_id='501',
        status='succeeded', remote_name='Original remote channel', models=['original-model'],
        upload_template_id=template.id, template_version=template.version,
        template_snapshot={'site_id': site['id'], 'template_id': template.id, 'version': template.version},
        remote_snapshot={'id': '501', 'name': 'Original remote channel'})
    db.add(distribution)
    db.flush()
    db.add_all([
        ChannelCredential(owner_id=owner.id, channel_id=channel.id, ordinal=0,
                          fingerprint=channel.fingerprint),
        KeyVersion(channel_id=channel.id, version=1, key_encrypted=channel.key_encrypted,
                   fingerprint=channel.fingerprint),
        DistributionVersion(distribution_id=distribution.id, key_version=1),
        TaskItem(task_id=task.id, channel_id=channel.id, site_id=site['id'],
                 distribution_id=distribution.id, operation='create', status='succeeded',
                 stage='reconcile', key_version=1, remote_write_attempted=True,
                 proxy_encrypted=encrypt('http://original-proxy.invalid'), attempts=1,
                 snapshot={'site_id': site['id'], 'remote_id': '501', 'template_id': template.id}),
        UnclaimedChannel(site_id=site['id'], remote_id='502', remote_name='Original unclaimed',
                         snapshot={'id': '502', 'site_id': site['id']}),
    ])
    db.commit()


def test_repeated_api_recreation_preserves_archived_credentials_templates_and_history(db, login, users, catalog):
    root = login('root')
    original = create(root, enabled=True, routing_group='original-group')
    original_templates = templates(db, original['id'])
    openai = db.scalar(select(Category).where(Category.family == 'OpenAI'))
    configured = next(row for row in original_templates if row.category_id == openai.id)
    configured.enabled, configured.models = True, ['original-model']
    configured.routing_group, configured.channel_config = 'configured-group', {'status': 1}
    configured.model_mapping = {'original-model': 'original-alias'}
    configured.model_rpm_requirements = {'original-model': 15}
    configured.proxy_encrypted = encrypt('http://original-proxy.invalid')
    configured.remark, configured.version = 'Keep the old configuration', 7
    add_history(db, original, configured, users['root'])
    historical = table_snapshot(db, *HISTORY_MODELS)
    initial_audits = table_snapshot(db, AuditEvent)['audit_events']
    expected_variants = {(row.category_id, row.format_id, row.variant) for row in original_templates}
    assert len(expected_variants) == 11

    archived = {}
    seen_site_ids, seen_numbers = {original['id']}, {original['display_id']}
    seen_template_ids = {row.id for row in original_templates}
    seen_template_numbers = {row.display_id for row in original_templates}
    current = original
    for generation in range(1, 4):
        before = row_snapshot(db.get(Site, current['id']))
        before_templates = [row_snapshot(row) for row in templates(db, current['id'])]
        archive(root, current)
        db.expire_all()
        archived[current['id']] = ({**before, 'archived': True, 'enabled': False}, before_templates)
        assert row_snapshot(db.get(Site, current['id'])) == archived[current['id']][0]

        current = create(root, name=f'Recreated {generation}', token=f'replacement-token-{generation}',
                         seller_user_id=str(80 + generation), routing_group='new-group', enabled=True)
        assert current['id'] not in seen_site_ids and current['display_id'] > max(seen_numbers)
        seen_site_ids.add(current['id'])
        seen_numbers.add(current['display_id'])
        new_templates = templates(db, current['id'])
        assert {(row.category_id, row.format_id, row.variant) for row in new_templates} == expected_variants
        assert seen_template_ids.isdisjoint(row.id for row in new_templates)
        assert seen_template_numbers.isdisjoint(row.display_id for row in new_templates)
        seen_template_ids.update(row.id for row in new_templates)
        seen_template_numbers.update(row.display_id for row in new_templates)
        assert all(row.enabled is False and row.models == [] and row.routing_group == 'new-group'
                   and row.channel_config == {'status': 2} and row.proxy_encrypted is None
                   and row.model_mapping == row.model_rpm_requirements == row.model_tpm_requirements == {}
                   and row.remark == '' and row.version == 1 for row in new_templates)
        assert decrypt(db.get(Site, current['id']).token_encrypted) == f'replacement-token-{generation}'
        assert table_snapshot(db, *HISTORY_MODELS) == historical
        for old_id, (old_site, old_templates) in archived.items():
            assert row_snapshot(db.get(Site, old_id)) == old_site
            assert [row_snapshot(row) for row in templates(db, old_id)] == old_templates
        assert all(row_snapshot(db.get(AuditEvent, event['id'])) == event for event in initial_audits)
        visible = root.get('/api/sites').json()['items']
        assert len(visible) == 1 and visible[0]['id'] == current['id']
        assert visible[0]['remote_channels'] == 0 and visible[0]['unfinished_tasks'] == 0

    assert decrypt(db.get(Site, original['id']).token_encrypted) == 'reusable-token'
    assert root.patch('/api/sites/' + original['id'], json={'name': 'Must remain archived'}).status_code == 404
    assert len(table_snapshot(db, Site)['sites']) == 4


@pytest.mark.parametrize('conflict', ['prefix', 'base_url', 'both'])
def test_active_replacement_still_rejects_conflicting_creates_without_orphan_rows(db, login, catalog, conflict):
    root = login('root')
    archived = create(root)
    archive(root, archived)
    active = create(root)
    before = table_snapshot(db, Site, SiteUploadTemplate, AuditEvent)
    body = site_body('conflicting', enabled=True)
    for field in ('prefix', 'base_url') if conflict == 'both' else (conflict,):
        body[field] = active[field]
    response = root.post('/api/sites', json=body)
    assert response.status_code == 409, response.text
    assert table_snapshot(db, Site, SiteUploadTemplate, AuditEvent) == before
    assert root.get('/api/sites').json()['total'] == 1


@pytest.mark.parametrize('conflict', ['prefix', 'base_url', 'both'])
def test_conflicting_patch_rolls_back_all_site_changes_and_creates_no_templates_or_audit(db, login, catalog, conflict):
    root = login('root')
    owner = create(root, 'identity-owner')
    target = create(root, 'patch-target', enabled=True)
    before = table_snapshot(db, Site, SiteUploadTemplate, AuditEvent)
    body = {'name': 'Must roll back', 'token': 'must-not-replace-token', 'seller_user_id': '93',
            'routing_group': 'must-roll-back', 'enabled': False}
    for field in ('prefix', 'base_url') if conflict == 'both' else (conflict,):
        body[field] = owner[field]
    response = root.patch('/api/sites/' + target['id'], json=body)
    assert response.status_code == 409, response.text
    assert table_snapshot(db, Site, SiteUploadTemplate, AuditEvent) == before
    assert decrypt(db.get(Site, target['id']).token_encrypted) == 'patch-target-token'
    recovery = root.patch('/api/sites/' + target['id'], json={'name': 'Valid follow-up'})
    assert recovery.status_code == 200 and recovery.json()['name'] == 'Valid follow-up'


@pytest.mark.parametrize('reused_field', ['prefix', 'base_url'])
def test_archived_prefix_and_address_can_each_be_reused_independently(db, login, catalog, reused_field):
    root = login('root')
    original = create(root)
    archive(root, original)
    before = row_snapshot(db.get(Site, original['id']))
    replacement = create(root, 'different-identity', **{reused_field: original[reused_field]})
    assert replacement[reused_field] == original[reused_field]
    other_field = 'base_url' if reused_field == 'prefix' else 'prefix'
    assert replacement[other_field] != original[other_field]
    assert replacement['id'] != original['id'] and replacement['display_id'] != original['display_id']
    db.expire_all()
    assert row_snapshot(db.get(Site, original['id'])) == before
    assert len(templates(db, replacement['id'])) == 11


def test_concurrent_creates_of_released_identity_have_one_winner_and_no_orphans(db, login, catalog, mock_site_api):
    clients = [login('root'), login('root')]
    original = create(clients[0])
    archive(clients[0], original)
    before = row_snapshot(db.get(Site, original['id']))
    original_templates = [row_snapshot(row) for row in templates(db, original['id'])]
    mock_site_api.barrier = Barrier(2)

    def contender(index):
        return clients[index].post('/api/sites', json=site_body(name=f'Contender {index}'))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(contender, range(2)))
    finally:
        mock_site_api.barrier = None
    assert sorted(response.status_code for response in responses) == [201, 409], [
        (response.status_code, response.text) for response in responses]
    winner = next(response.json() for response in responses if response.status_code == 201)
    snapshot = table_snapshot(db, Site, SiteUploadTemplate, AuditEvent)
    assert len(snapshot['sites']) == 2 and len(snapshot['site_upload_templates']) == 22
    assert row_snapshot(db.get(Site, original['id'])) == before
    assert [row_snapshot(row) for row in templates(db, original['id'])] == original_templates
    assert {event['object_id'] for event in snapshot['audit_events'] if event['action'] == 'site.create'} == {
        original['id'], winner['id']}
    assert {event['object_id'] for event in snapshot['audit_events'] if event['action'] == 'upload_template.create'} == {
        row['id'] for row in snapshot['site_upload_templates']}
    active = [row for row in snapshot['sites'] if not row['archived']]
    assert len(active) == 1 and active[0]['id'] == winner['id']
    assert all(row.enabled is False for row in templates(db, winner['id']))
