"""Acceptance tests use placeholder secrets and in-memory seller transport only."""
from types import SimpleNamespace

import httpx
import pytest
from app import worker
from app.adapters.silicon import RemoteError, SiliconAdapter
from app.channel_service import parse_rows
from app.db import engine, uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_billing import SettlementOrder, SettlementOrderLine, UsageFact
from app.models_channels import (
    Channel,
    Distribution,
    DistributionVersion,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from app.security import encrypt
from settlement_fixtures import order_for
from sqlalchemy import event, func, select


@pytest.fixture
def setup_catalog(db, users, monkeypatch):
    monkeypatch.setattr('app.routers.uploads.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.channels.notify_worker', lambda: None)
    monkeypatch.setattr('app.routers.tasks.notify_worker', lambda: None)
    category = Category(id=uid(), name='OpenAI', family='OpenAI', active=True)
    db.add(category)
    db.flush()
    fmt = CredentialFormat(id=uid(), category_id=category.id, name='API Key v1', code='api_key-v1', version='1',
                           enabled=True, schema_config={'type': 'api_key', 'remote_type': 1}, default_models=['test-model'])
    db.add(fmt)
    sites = []
    for index in range(3):
        site = Site(id=uid(), name=f'Test {index}', prefix=f'T{index}', base_url=f'https://seller-{index}.example',
                    adapter='silicon-v1', seller_user_id='123', token_encrypted=encrypt('test-only-seller-token'),
                    enabled=True, verified_at=utcnow(), health='healthy', capabilities={
                        'can_write': True, 'can_toggle': True, 'create': 'supported', 'edit': 'supported', 'toggle': 'supported',
                        'models': ['test-model'], 'groups': ['default'], 'formats': ['api_key-v1'], 'test': 'unsupported'})
        db.add(site)
        sites.append(site)
    db.commit()
    return category, fmt, sites


def payload(catalog, keys='placeholder-key-alpha', nonce='test-upload-0001', **kwargs):
    category, fmt, _ = catalog
    return {'category_id': category.id, 'format_id': fmt.id, 'credentials': keys,
            'models': ['test-model'], 'idempotency_key': nonce, **kwargs}


def test_bind_before_dedup_preserves_original_rows():
    rows, errors = parse_rows('\n a-key \nb-key\na-key\n', 'first\nsecond\nfirst', '', 100)
    assert errors == []
    assert [(r['line'], r['remark'], r['status']) for r in rows] == [(2, 'first', 'valid'), (3, 'second', 'valid'), (4, 'first', 'duplicate')]
    rows, _ = parse_rows('a-key\nb-key\na-key', 'one\ntwo\nDIFFERENT')
    assert [r['status'] for r in rows] == ['conflict', 'valid', 'conflict']


def test_empty_internal_key_and_metadata_mismatch():
    rows, errors = parse_rows('a-key\n\nb-key', 'one\ntwo')
    assert rows[1]['line'] == 2 and rows[1]['status'] == 'invalid'
    assert any('需要 3 行，实际 2 行' in error for error in errors)
    rows, errors = parse_rows('\n \n')
    assert rows == [] and errors


def test_batch_has_one_group_five_channels_fifteen_items_and_is_idempotent(db, login, setup_catalog):
    client = login('admin')
    body = payload(setup_catalog, '\n'.join(f'placeholder-key-{i}' for i in range(5)))
    result = client.post('/api/uploads/submit', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 15
    repeat = client.post('/api/uploads/submit', json=body)
    assert repeat.json()['id'] == result.json()['id']
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1
    assert db.scalar(select(func.count()).select_from(Channel)) == 5
    assert db.scalar(select(func.count()).select_from(TaskItem)) == 15
    conflict = client.post('/api/uploads/submit', json={**body, 'remarks': 'Changed'})
    assert conflict.status_code == 409
    assert 'placeholder-key' not in result.text


def test_existing_keys_keep_group_no_empty_group(db, login, setup_catalog):
    client = login('admin')
    first = client.post('/api/uploads/submit', json=payload(setup_catalog)).json()
    old_group = first['group_id']
    for item in db.scalars(select(TaskItem)):
        item.status = 'succeeded'
    for task in db.scalars(select(Task)):
        task.status = 'succeeded'
    site = Site(id=uid(), name='Fourth', prefix='F', base_url='https://fourth.example', seller_user_id='123',
                token_encrypted=encrypt('placeholder-token'), enabled=True, verified_at=utcnow(),
                capabilities=setup_catalog[2][0].capabilities)
    db.add(site)
    db.commit()
    second = client.post('/api/uploads/submit', json=payload(setup_catalog, nonce='test-upload-0002'))
    assert second.status_code == 200, second.text
    assert second.json()['group_id'] is None and second.json()['total'] == 1
    assert db.scalar(select(func.count()).select_from(UploadGroup)) == 1
    assert db.scalar(select(Channel)).group_id == old_group
    third = client.post('/api/uploads/submit', json=payload(setup_catalog, nonce='test-upload-0003'))
    assert third.status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 2


def test_unsupported_options_visible_and_do_not_silently_drop(db, login, setup_catalog):
    client = login('user')
    body = payload(setup_catalog, rpm_enabled=True, rpm_limit=60)
    preview = client.post('/api/uploads/preview', json={k: v for k, v in body.items() if k != 'idempotency_key'}).json()
    assert not preview['can_submit']
    assert len(preview['targets']) == 3
    assert all(any('RPM' in s for s in row['issues']) for row in preview['targets'])
    assert client.post('/api/uploads/submit', json=body).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_local_rbac_channel_tasks_and_reveal(db, login, setup_catalog):
    owned = login('user')
    result = owned.post('/api/uploads/submit', json=payload(setup_catalog)).json()
    channel_id = result['items'][0]['channel_id']
    other = login('other_admin')
    assert other.get('/api/channels/' + channel_id).status_code == 404
    assert other.post('/api/channels/' + channel_id + '/reveal').status_code == 404
    assert other.get('/api/tasks/' + result['id']).status_code == 403
    parent = login('admin')
    assert parent.post('/api/channels/' + channel_id + '/reveal').json()['key'] == 'placeholder-key-alpha'
    assert 'placeholder-key-alpha' not in parent.get('/api/channels').text
    assert login('root').post('/api/uploads/submit', json=payload(setup_catalog)).status_code == 403


def test_retry_only_failed_and_keeps_submission_idempotency(db, login, setup_catalog):
    client = login('user')
    body = payload(setup_catalog)
    result = client.post('/api/uploads/submit', json=body).json()
    items = list(db.scalars(select(TaskItem).where(TaskItem.task_id == result['id'])))
    items[0].status = items[1].status = 'succeeded'
    items[2].status = 'failed'
    task = db.get(Task, result['id'])
    task.status = 'partial'
    db.commit()
    assert login('admin').post('/api/tasks/' + task.id + '/retry').status_code == 403
    retried = login('root').post('/api/tasks/' + task.id + '/retry')
    assert retried.status_code == 200, retried.text
    assert retried.json()['counts'] == {'succeeded': 2, 'pending': 1}
    assert client.post('/api/uploads/submit', json=body).json()['id'] == task.id


def adapter_site():
    return SimpleNamespace(base_url='https://mock.example', seller_user_id='123', token_encrypted=encrypt('placeholder-token'))


def response(data=None, success=True, status=200):
    return httpx.Response(status, json={'success': success, 'data': data}, request=httpx.Request('GET', 'https://mock.example'))


def test_adapter_wrapping_and_business_failure_and_no_response_leak():
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith('/api/user/self'):
            return response({'id': 123})
        if url.endswith('/api/status'):
            return response({'version': SiliconAdapter.VERSION})
        if url.endswith('/meta'):
            return response({'models': [{'id': 'test-model'}], 'groups': ['default']})
        if method == 'GET':
            return response({'can_write': True, 'can_toggle': True, 'can_edit_routing': False, 'items': [], 'total': 0})
        return response()
    adapter = SiliconAdapter(adapter_site(), transport=transport)
    assert adapter.create(name='stable-name', key='placeholder-key', models=['test-model']) is None
    sent = calls[-1][2]['json']
    assert sent['mode'] == 'single' and sent['channel']['status'] == 2
    assert sent['channel']['settings'] == {} and sent['channel']['proxy'] == ''
    assert 'setting' not in sent['channel']
    assert 'priority' not in sent['channel'] and 'weight' not in sent['channel']
    adapter.transport = lambda *a, **kw: response({'secret': 'sensitive'}, success=False)
    with pytest.raises(RemoteError) as caught:
        adapter.permissions()
    assert 'sensitive' not in str(caught.value)


def test_adapter_edit_omits_masked_key_and_unauthorized_routing():
    sent = []
    def transport(method, url, **kwargs):
        if url.endswith('/api/user/self'):
            return response({'id': 123})
        if url.endswith('/meta'):
            return response({'models': [{'id': 'test-model'}], 'groups': ['default']})
        if url.endswith('/api/status'):
            return response({'version': SiliconAdapter.VERSION})
        if method == 'PUT':
            sent.append(kwargs['json'])
            return response()
        if url.endswith('/77'):
            return response({'id': 77, 'name': 'before', 'type': 1, 'key': 'masked', 'priority': 7, 'weight': 4,
                             'status': 1, 'settings': '{}', 'proxy': '', 'group': 'default', 'models': 'test-model'})
        return response({'can_write': True, 'can_edit_routing': False, 'can_toggle': True, 'items': [], 'total': 0})
    adapter = SiliconAdapter(adapter_site(), transport=transport)
    adapter.edit('77', changes={'remark': 'changed'})
    assert not {'key', 'priority', 'weight', 'status'} & sent[0].keys()
    assert sent[0]['remark'] == 'changed'


def test_worker_reconciles_unknown_create_without_reposting(db, login, setup_catalog, monkeypatch):
    result = login('user').post('/api/uploads/submit', json=payload(setup_catalog)).json()
    item = db.get(TaskItem, result['items'][0]['id'])
    dist = db.get(Distribution, item.distribution_id)
    item.stage = 'create_sent'
    db.commit()
    class MockAdapter:
        def __init__(self, *args, **kwargs): pass
        def find_unique_name(self, name):
            return {'id': 77, 'name': name, 'type': 1, 'status': 2, 'models': 'test-model', 'group': 'default'}
        def create(self, **kwargs):
            pytest.fail('Unknown create must never be posted again')
    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    worker.execute_item(db, item)
    assert dist.remote_id == '77' and dist.status == 'disabled'


def test_worker_absent_unknown_stays_unresolved(db, login, setup_catalog, monkeypatch):
    result = login('user').post('/api/uploads/submit', json=payload(setup_catalog)).json()
    item = db.get(TaskItem, result['items'][0]['id'])
    item.stage = 'create_sent'
    db.commit()
    class MockAdapter:
        def __init__(self, *args, **kwargs): pass
        def find_unique_name(self, name): return None
        def create(self, **kwargs): pytest.fail('No blind retry')
    monkeypatch.setattr(worker, 'get_adapter', MockAdapter)
    with pytest.raises(RemoteError) as caught:
        worker.execute_item(db, item)
    assert caught.value.unknown


def test_worker_rechecks_actor_and_disabled_site_before_write(db, users, login, setup_catalog, monkeypatch):
    result = login('user').post('/api/uploads/submit', json=payload(setup_catalog)).json()
    item = db.get(TaskItem, result['items'][0]['id'])
    users['user'].session_version += 1
    db.commit()
    with pytest.raises(worker.WriteStopped):
        worker.execute_item(db, item)
    users['user'].session_version -= 1
    site = db.get(Site, item.site_id)
    site.enabled = False
    db.commit()
    with pytest.raises(worker.WriteStopped):
        worker.execute_item(db, item)


def test_acknowledged_create_lookup_failure_never_becomes_retryable_post(db, login, setup_catalog, monkeypatch):
    client = login('user')
    result = client.post('/api/uploads/submit', json=payload(setup_catalog)).json()
    item_id = result['items'][0]['id']
    item = db.get(TaskItem, item_id)
    dist = db.get(Distribution, item.distribution_id)
    name = dist.remote_name
    for other in db.scalars(select(TaskItem).where(TaskItem.id != item_id)):
        other.status = 'succeeded'
    db.commit()
    writes, acknowledged, return_match = [], False, False

    def transport(method, url, **kwargs):
        nonlocal acknowledged
        if url.endswith('/api/user/self'):
            return response({'id': 123})
        if url.endswith('/api/status'):
            return response({'version': SiliconAdapter.VERSION})
        if method == 'POST':
            writes.append(kwargs['json'])
            acknowledged = True
            return response()  # Provider accepted create, but omitted the new ID.
        if url.endswith('/meta'):
            return response({'models': [{'id': 'test-model'}], 'groups': ['default']})
        if acknowledged and not return_match:
            return response(status=503)
        if return_match:
            return response({'items': [{'id': 991, 'name': name, 'type': 1, 'status': 2, 'models': 'test-model', 'group': 'default'}],
                             'total': 1, 'can_write': True, 'can_toggle': True, 'can_edit_routing': False})
        return response({'can_write': True, 'can_toggle': True, 'can_edit_routing': False, 'items': [], 'total': 0})

    class TestLease:
        def __init__(self, *args): pass
        def acquire(self): return True
        def check(self): pass
        def close(self): pass

    original_adapter = SiliconAdapter
    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write=None: original_adapter(site, transport=transport, before_write=before_write))
    monkeypatch.setattr(worker, 'SiteLease', TestLease)
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item_id)
    assert item.status == 'needs_review' and item.stage == 'created_pending_verification'
    assert len(writes) == 1
    assert client.post('/api/tasks/' + result['id'] + '/retry').status_code == 409
    return_match = True
    assert client.post('/api/tasks/' + result['id'] + '/reconcile').status_code == 200
    db.commit()
    assert worker.run_once(client=object())
    db.expire_all()
    assert db.get(TaskItem, item_id).status == 'succeeded'
    assert len(writes) == 1


def test_imported_channel_and_site_usage_totals_and_scope(db, users, login, setup_catalog):
    client = login('user')
    own = client.post('/api/uploads/submit', json=payload(setup_catalog)).json()
    foreign = login('other_user').post('/api/uploads/submit', json=payload(setup_catalog, keys='unrelated-placeholder-key')).json()
    channel_id = own['items'][0]['channel_id']
    for dist in db.scalars(select(Distribution)):
        dist.remote_id = str(1000 + len(dist.id)) + '-' + dist.id
    db.commit()
    stamp = utcnow().isoformat() + 'Z'

    def fact(distribution_id, amount, source_id, *, unit='USD', verified=True):
        return {'distribution_id': distribution_id, 'source_id': source_id, 'occurred_at': stamp,
                'raw_amount': amount, 'raw_unit': unit, 'amount': amount if verified else None,
                'unit': unit, 'verified': verified, 'conversion_version': 'test-conversion-v1',
                'evidence': 'Local fixture invoice evidence only'}

    root = login('root')
    body = {'items': [fact(i['distribution_id'], str(amount), f'invoice-{amount}')
                      for i, amount in zip(own['items'], [10, 20, 30])]
                     + [fact(foreign['items'][0]['distribution_id'], '9999', 'other-owner-invoice')]}
    imported = root.post('/api/usage/import', json=body)
    assert imported.status_code == 200, imported.text
    result = client.get('/api/channels/' + channel_id).json()
    assert result['usage_by_unit'] == {'USD': '60.00000000'}
    assert result['verified_usage_by_unit'] == {'USD': '60.00000000'}
    assert result['coverage'] == {'covered': 3, 'total': 3}
    assert result['source_cutoff'] is not None
    assert {d['site_id'] for d in result['distributions']} == {i['site_id'] for i in own['items']}
    assert sorted(float(d['usage_by_unit']['USD']) for d in result['distributions']) == [10, 20, 30]
    assert client.get('/api/channels/' + foreign['items'][0]['channel_id']).status_code == 404
    assert '9999' not in client.get('/api/channels').text
    additional = {'items': [fact(own['items'][0]['distribution_id'], '777', 'raw-only', unit='quota', verified=False),
                            fact(own['items'][1]['distribution_id'], '7', 'eur-fact', unit='EUR')]}
    assert root.post('/api/usage/import', json=additional).status_code == 200
    result = client.get('/api/channels/' + channel_id).json()
    assert result['usage_by_unit'] == {'USD': '60.00000000', 'EUR': '7.00000000'}
    assert result['raw_usage_by_unit']['quota'] == '777.00000000'
    assert result['unverified_count'] == 1 and result['data_status'] == 'partial'
    assert result['coverage'] == {'covered': 3, 'total': 3}  # Not number of imported facts.


def test_channel_list_query_count_does_not_grow_per_channel(db, login, setup_catalog):
    client = login('user')
    body = payload(setup_catalog, keys='\n'.join(f'placeholder-many-keys-{i}' for i in range(20)))
    assert client.post('/api/uploads/submit', json=body).status_code == 200

    def query_count(limit):
        statements = []
        def record(*args):
            statements.append(args[2])
        event.listen(engine, 'before_cursor_execute', record)
        try:
            response = client.get(f'/api/channels?limit={limit}')
            assert response.status_code == 200
            assert len(response.json()['items']) == limit
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        return len(statements)

    assert query_count(20) <= query_count(1) + 1


@pytest.fixture
def delete_case(db, login, setup_catalog, monkeypatch):
    client = login('user')
    uploaded = client.post('/api/uploads/submit', json=payload(setup_catalog)).json()
    for item in db.scalars(select(TaskItem)):
        item.status = 'succeeded'
    db.get(Task, uploaded['id']).status = 'succeeded'
    distributions = list(db.scalars(select(Distribution).where(Distribution.channel_id == uploaded['items'][0]['channel_id'])))
    for index, dist in enumerate(distributions):
        dist.remote_id, dist.status = str(701 + index), 'missing'
        dist.remote_snapshot = {'id': 701 + index, 'name': dist.remote_name, 'type': 1,
                                'models': 'test-model', 'group': 'default', 'status': 2}
    db.commit()

    class TestLease:
        def __init__(self, *args): pass
        def acquire(self): return True
        def check(self): pass
        def close(self): pass

    monkeypatch.setattr(worker, 'SiteLease', TestLease)
    dist = distributions[0]
    state = SimpleNamespace(client=client, channel=db.get(Channel, dist.channel_id), dist=dist,
                            remote=dict(dist.remote_snapshot), writes=[], detail_status=200, lose_ack=False,
                            detail_reads=0, on_detail=lambda: None)

    def transport(method, url, **kwargs):
        if method == 'DELETE':
            state.writes.append(url)
            if state.lose_ack:
                raise httpx.ReadTimeout('Test timeout, no live request')
            return response()
        if url.endswith('/api/user/self'):
            return response({'id': 123})
        if url.endswith('/meta'):
            return response({'models': [{'id': 'test-model'}], 'groups': ['default']})
        if url.endswith('/api/status'):
            return response({'version': SiliconAdapter.VERSION})
        if url.endswith('/' + dist.remote_id):
            state.detail_reads += 1
            state.on_detail()
            return response(state.remote, status=state.detail_status)
        if url.endswith('/api/seller/channel/'):
            return response({'can_write': True, 'can_toggle': True, 'can_edit_routing': False, 'items': [], 'total': 0})
        pytest.fail('Unexpected mock request')

    monkeypatch.setattr(worker, 'get_adapter', lambda site, before_write=None: SiliconAdapter(
        site, transport=transport, before_write=before_write))
    return state


def queue_delete(db, case):
    result = case.client.post('/api/channels/' + case.channel.id + '/actions', json={
        'action': 'delete_remote', 'site_ids': [case.dist.site_id], 'confirmation': 'DELETE 1'})
    assert result.status_code == 200, result.text
    return db.get(TaskItem, result.json()['items'][0]['id'])


def test_delete_scope_confirmation_and_owner_permissions(db, login, delete_case):
    case = delete_case
    path = '/api/channels/' + case.channel.id + '/actions'
    request = {'action': 'delete_remote', 'site_ids': [case.dist.site_id], 'confirmation': 'DELETE 1'}
    before = db.scalar(select(func.count()).select_from(Task))
    for who in ('other_user', 'other_admin', 'sibling'):
        assert login(who).post(path, json=request).status_code == 404
    assert case.client.post(path, json={**request, 'confirmation': ''}).status_code == 422
    assert case.client.post(path, json={**request, 'site_ids': [uid()]}).status_code == 422
    assert db.scalar(select(func.count()).select_from(Task)) == before
    result = login('admin').post(path, json=request)
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 1
    item = db.get(TaskItem, result.json()['items'][0]['id'])
    assert item.snapshot['delete_target'] == {'id': case.dist.remote_id, 'name': case.dist.remote_name, 'type': 1}
    assert item.distribution_id == case.dist.id and not case.writes


def test_delete_missing_remote_checks_identity_and_preserves_usage_settlement_history(db, users, delete_case):
    case = delete_case
    now = utcnow()
    history = DistributionVersion(distribution_id=case.dist.id, key_version=1)
    fact = UsageFact(id=uid(), source_id='deletion-history-fixture', site_id=case.dist.site_id,
        distribution_id=case.dist.id, channel_id=case.channel.id, owner_id=users['user'].id,
        admin_id=users['admin'].id, category_id=case.channel.category_id, occurred_at=now,
        raw_amount=8, raw_unit='USD', amount=8, unit='USD', conversion_version='fixture', verified=True)
    db.add_all([history, fact])
    db.flush()
    order, line = order_for(db, users, case.channel, sources=[{'fact_id': fact.id}])
    db.commit()
    item = queue_delete(db, case)
    worker.execute_item(db, item)
    assert len(case.writes) == 1
    assert case.dist.status == 'deleted' and case.dist.remote_id == '701'
    assert item.remote_write_attempted and item.snapshot['delete_acknowledged']
    assert db.get(DistributionVersion, history.id).valid_to is not None
    for model, row in [(Channel, case.channel), (Distribution, case.dist), (UsageFact, fact),
                       (SettlementOrder, order), (SettlementOrderLine, line)]:
        assert db.get(model, row.id) is not None
    assert db.get(SettlementOrderLine, line.id).site_amounts == [{'fact_id': fact.id}]
    assert db.get(SettlementOrder, order.id).status == 'settled'
    assert db.get(UsageFact, fact.id).amount == 8
    assert db.scalar(select(func.count()).select_from(KeyVersion)) == 1
    assert case.client.get('/api/channels/' + case.channel.id).json()['usage_by_unit'] == {'USD': '8.00000000'}
    # A worker restart after the durable acknowledgement cannot repeat DELETE.
    worker.execute_item(db, item)
    assert len(case.writes) == 1


@pytest.mark.parametrize('changed', [{'name': 'another-channel'}, {'type': 14}, {'id': 999}])
def test_delete_identity_mismatch_never_sends_delete(db, delete_case, changed):
    case = delete_case
    case.remote.update(changed)
    item = queue_delete(db, case)
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == 'needs_review' and not item.remote_write_attempted
    assert not case.writes and db.get(Distribution, case.dist.id).status != 'deleted'


@pytest.mark.parametrize('change', ['name', 'missing'])
def test_delete_rechecks_remote_identity_after_permission_reads(db, delete_case, change):
    case = delete_case

    def on_detail():
        if case.detail_reads == 2:
            if change == 'name':
                case.remote['name'] = 'changed-after-first-read'
            else:
                case.detail_status = 404

    case.on_detail = on_detail
    item = queue_delete(db, case)
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == 'needs_review' and not item.remote_write_attempted
    assert case.detail_reads == 2 and not case.writes


@pytest.mark.parametrize('detail_status', [403, 404, 503])
def test_delete_ambiguous_absence_or_permission_failure_never_means_deleted(db, delete_case, detail_status):
    case = delete_case
    case.detail_status = detail_status
    item = queue_delete(db, case)
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == 'needs_review' and not item.remote_write_attempted
    assert not case.writes and db.get(Distribution, case.dist.id).status != 'deleted'
    assert case.client.post('/api/tasks/' + item.task_id + '/retry').status_code == 409


@pytest.mark.parametrize('change', ['session', 'site', 'association'])
def test_delete_rechecks_local_authorization_and_frozen_association_immediately_before_write(db, users, delete_case, change):
    case = delete_case
    item = queue_delete(db, case)
    checks = 0

    def lease_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            if change == 'session':
                users['user'].session_version += 1
            elif change == 'site':
                db.get(Site, case.dist.site_id).enabled = False
            else:
                case.dist.remote_id = '999'
            db.commit()

    with pytest.raises(worker.WriteStopped):
        worker.execute_item(db, item, lease_check)
    assert not item.remote_write_attempted and not case.writes


@pytest.mark.parametrize('still_visible', [True, False])
def test_unknown_delete_is_read_only_reconciled_without_blind_resend(db, delete_case, still_visible):
    case = delete_case
    case.lose_ack = True
    item = queue_delete(db, case)
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == 'needs_review' and item.remote_write_attempted
    assert len(case.writes) == 1
    assert case.client.post('/api/tasks/' + item.task_id + '/retry').status_code == 409
    case.detail_status = 200 if still_visible else 404
    assert case.client.post('/api/tasks/' + item.task_id + '/reconcile').status_code == 200
    assert worker.run_once(client=object())
    db.expire_all()
    item = db.get(TaskItem, item.id)
    assert item.status == ('failed' if still_visible else 'needs_review')
    assert db.get(Distribution, case.dist.id).status != 'deleted'
    assert len(case.writes) == 1
