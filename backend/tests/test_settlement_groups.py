"""Account-scoped settlement listings expose safe, read-only channel group metadata."""
import json
from copy import deepcopy
from decimal import Decimal

import pytest
from app.db import engine, uid
from app.models import AuditEvent, Site
from app.models_billing import SettlementOrder, SettlementOrderLine, UsageFact
from app.models_channels import Channel, Distribution, Task, TaskItem, UploadGroup
from app.security import encrypt
from app.settlement_orders import order_group_status
from settlement_fixtures import _add_group
from settlement_fixtures import ledger as _ledger_fixture
from sqlalchemy import event, func, select
from test_channel_categories import service_case as _service_fixture
from test_remote_usage_totals import STAMP, VERSION, conversion, manual_proof
from test_remote_usage_totals import distribution as remote_distribution
from test_settlement_orders import _create, _detail

ledger = _ledger_fixture
service_case = _service_fixture


def _list(api, account, **params):
    result = api.get('/api/settlements/groups', params={'account_id': account.id, **params})
    assert result.status_code == 200, result.text
    return result.json()


def _ids(data):
    return {row['id'] for row in data['items']}


def _owner_ids(db, *owners):
    return set(db.scalars(select(Channel.id).where(Channel.owner_id.in_([owner.id for owner in owners]))))


def _observe(dist, quota):
    sample = remote_distribution(dist.remote_id, quota=quota, site_id=dist.site_id)
    dist.status = sample.status
    dist.remote_snapshot = sample.remote_snapshot
    dist.last_sync_at = sample.last_sync_at


def _extra_distribution(db, channel_id, site_id, *, remote_id=None):
    dist = Distribution(channel_id=channel_id, site_id=site_id, remote_id=remote_id or uid(),
        remote_name='fixture-remote-total', partition_key=uid())
    db.add(dist)
    db.flush()
    return dist


def _row(data, channel_id):
    return next(row for row in data['items'] if row['id'] == channel_id)


def test_root_admin_account_includes_only_own_and_direct_users_even_when_archived(db, users, ledger, login):
    archived, _ = _add_group(db, ledger, users['sibling'], admin_id=users['admin'].id)
    archived.archived = True
    users['sibling'].archived = True
    foreign, _ = _add_group(db, ledger, users['other_user'], admin_id=users['other_admin'].id)
    # An administrator is never included as a subordinate ordinary user.
    users['other_admin'].parent_id = users['admin'].id
    nested_admin, _ = _add_group(db, ledger, users['other_admin'], admin_id=users['other_admin'].id)
    db.commit()
    data = _list(login('root'), users['admin'])
    expected = _owner_ids(db, users['admin'], users['user'], users['sibling'])
    assert _ids(data) == expected and data['total'] == len(expected)
    assert foreign.id not in _ids(data) and nested_admin.id not in _ids(data)
    assert next(row for row in data['items'] if row['id'] == archived.id)['archived'] is True
    assert data['account'] == {'id': users['admin'].id, 'username': users['admin'].username,
        'nickname': users['admin'].nickname, 'role': 'admin'}
    assert all(row['settlement']['layer'] == 'upper' for row in data['items'])
    assert all(row['settlement']['payee_id'] == users['admin'].id for row in data['items'])


def test_root_ordinary_account_is_exact_and_preserves_parent_specific_layer(db, users, ledger, login):
    sibling, _ = _add_group(db, ledger, users['sibling'], admin_id=users['admin'].id)
    direct, direct_fact = _add_group(db, ledger, users['other_user'], admin_id=users['root'].id)
    _observe(db.get(Distribution, direct_fact.distribution_id), 15500000)
    users['other_user'].parent_id = users['root'].id
    db.commit()
    root = login('root')
    child = _list(root, users['user'])
    expected = _owner_ids(db, users['user'])
    assert _ids(child) == expected and child['total'] == len(expected)
    assert sibling.id not in _ids(child)
    for row in child['items']:
        assert row['owner_id'] == users['user'].id
        assert row['settlement']['layer'] == 'upper'
        assert row['settlement']['payee_id'] == users['admin'].id
        assert row['settlement']['can_manage'] is True
    own = _list(root, users['other_user'])
    assert _ids(own) == {direct.id} and own['total'] == 1
    settlement = own['items'][0]['settlement']
    assert (settlement['layer'], settlement['payee_id'], settlement['can_manage']) == (
        'lower', users['other_user'].id, True)
    assert Decimal(settlement['usage_amount']) == direct_fact.amount


def test_admin_lists_only_selected_direct_user_including_archived_channels(db, users, ledger, login):
    sibling, _ = _add_group(db, ledger, users['sibling'], admin_id=users['admin'].id)
    user_channel = db.scalar(select(Channel).where(Channel.owner_id == users['user'].id))
    user_channel.archived = True
    db.commit()
    data = _list(login('admin'), users['user'])
    expected = _owner_ids(db, users['user'])
    assert _ids(data) == expected and data['total'] == len(expected)
    assert sibling.id not in _ids(data)
    assert all(row['owner_id'] == users['user'].id for row in data['items'])
    assert next(row for row in data['items'] if row['id'] == user_channel.id)['archived'] is True
    for row in data['items']:
        assert (row['settlement']['layer'], row['settlement']['payee_id'], row['settlement']['can_manage']) == (
            'lower', users['user'].id, True)


def test_unauthorized_accounts_and_invalid_query_ranges_fail_before_listing(db, users, ledger, login):
    admin, root, user = login('admin'), login('root'), login('user')
    for account in (users['admin'].id, users['other_admin'].id, users['other_user'].id, users['root'].id, uid()):
        result = admin.get('/api/settlements/groups', params={'account_id': account})
        assert result.status_code == 404, result.text
    for account in (users['user'].id, users['admin'].id, users['other_user'].id):
        result = user.get('/api/settlements/groups', params={'account_id': account})
        assert result.status_code == 403, result.text
    for account in (uid(), users['root'].id):
        assert root.get('/api/settlements/groups', params={'account_id': account}).status_code == 404
    bad_params = [{}, {'account_id': ''}, {'account_id': 'x'*37}]
    bad_params += [{'account_id': users['admin'].id, **overrides} for overrides in (
        {'offset': -1}, {'limit': 0}, {'limit': 101}, {'search': 'x'*201})]
    for params in bad_params:
        result = root.get('/api/settlements/groups', params=params)
        assert result.status_code == 422, result.text
    assert db.scalar(select(func.count(SettlementOrder.id))) == 0
    assert db.scalar(select(func.count(SettlementOrderLine.id))) == 0


def test_pagination_has_stable_tiebreaks_complete_totals_and_empty_final_page(db, users, ledger, login):
    for _ in range(3):
        _add_group(db, ledger, users['user'], admin_id=users['admin'].id)
    owned = list(db.scalars(select(Channel).where(Channel.owner_id.in_([users['admin'].id, users['user'].id]))))
    for channel in owned:
        channel.created_at = ledger['now']
    db.commit()
    expected = sorted([row.id for row in owned], reverse=True)
    root = login('root')
    combined = []
    for offset in range(0, len(expected), 2):
        page = _list(root, users['admin'], offset=offset, limit=2)
        repeated = _list(root, users['admin'], offset=offset, limit=2)
        page_ids = [row['id'] for row in page['items']]
        assert page_ids == [row['id'] for row in repeated['items']]
        assert page_ids == expected[offset:offset+2]
        assert page['total'] == repeated['total'] == len(expected)
        combined.extend(page_ids)
    assert combined == expected and len(set(combined)) == len(expected)
    final = _list(root, users['admin'], offset=len(expected), limit=100)
    assert final['items'] == [] and final['total'] == len(expected)


def test_search_matches_safe_metadata_and_escapes_sql_wildcards(db, users, ledger, login):
    selected, _ = _add_group(db, ledger, users['user'], admin_id=users['admin'].id)
    other, _ = _add_group(db, ledger, users['user'], admin_id=users['admin'].id)
    # Random UUID tags could accidentally match a numeric display-id search.
    for index, existing in enumerate(db.scalars(select(UploadGroup).order_by(UploadGroup.id))):
        existing.tag = 'fixture-search-'+chr(ord('A')+index)
    group = db.get(UploadGroup, selected.group_id)
    group.tag = 'invoice%_needle'
    group.name = 'group-name-needle'
    group.remark = 'group-remark-needle'
    selected.remark = 'channel-remark-needle'
    db.get(UploadGroup, other.group_id).tag = 'invoiceXYneedle'
    db.commit()
    root = login('root')
    for term in ('invoice%_needle', 'group-name-needle', 'group-remark-needle', 'channel-remark-needle',
                 str(selected.display_id), f'#{selected.display_id}', '%', '_'):
        result = _list(root, users['admin'], search=term)
        assert _ids(result) == {selected.id}, (term, result)
        assert result['total'] == 1
    for term in (users['user'].username, users['user'].nickname):
        result = _list(root, users['admin'], search=term)
        expected = _owner_ids(db, users['user'])
        assert _ids(result) == expected and result['total'] == len(expected)
    absent = _list(root, users['admin'], search='nonexistent-group-needle')
    assert absent['items'] == [] and absent['total'] == 0


def test_list_metadata_cannot_leak_channel_credentials_or_raw_snapshots(db, users, ledger, login):
    channel = db.scalar(select(Channel).where(Channel.owner_id == users['user'].id))
    marker = 'synthetic-sensitive-fixture-never-public'
    channel.key_encrypted = marker+'-key'
    channel.key_hint = marker+'-hint'
    channel.fingerprint = marker+'-fingerprint'
    channel.proxy_encrypted = marker+'-proxy'
    channel.upload_settings = {'secret': marker+'-settings'}
    distribution = db.scalar(select(Distribution).where(Distribution.channel_id == channel.id))
    _observe(distribution, 1250000)
    distribution.remote_snapshot.update(token=marker+'-remote', key=marker+'-remote-key')
    fact = db.scalar(select(UsageFact).where(UsageFact.channel_id == channel.id))
    fact.evidence = marker+'-evidence'
    db.commit()
    data = _list(login('admin'), users['user'])
    assert marker not in json.dumps(data)
    assert _row(data, channel.id)['remote_usage_total'] == {
        'amount': '2.5', 'unit': 'USD', 'covered': 1, 'total': 1}
    fields = {'id', 'display_id', 'owner_id', 'owner_name', 'owner_username', 'group_id', 'group_tag',
        'category_id', 'category_name', 'created_at', 'archived', 'settlement', 'remote_usage_total',
        'variant', 'service_name', 'service_name_en'}
    for row in data['items']:
        assert set(row) == fields
        assert row['settlement']['channel_id'] == row['id']
    forbidden = {'key', 'key_encrypted', 'key_hint', 'fingerprint', 'proxy_encrypted', 'upload_settings',
        'remote_snapshot', 'template_snapshot', 'evidence', 'password_hash', 'recipient_account', 'token',
        'models', 'format_id', 'format_name', 'credential_format', 'schema_config'}

    def assert_safe(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                assert_safe(item)
        elif isinstance(value, list):
            for item in value:
                assert_safe(item)

    assert_safe(data)


def test_listing_reuses_settlement_projection_without_creating_or_claiming_anything(db, users, ledger, login):
    fact = next(row for row in ledger['facts'] if row.owner_id == users['user'].id
                and row.category_id == ledger['cats']['OpenAI'].id)
    dist = db.get(Distribution, fact.distribution_id)
    _observe(dist, 1250000)
    db.commit()
    root = login('root')
    order = _create(root, [db.get(Channel, fact.channel_id)])
    original_order = _detail(root, order)
    models = (SettlementOrder, SettlementOrderLine, UsageFact, AuditEvent, Distribution, Task, TaskItem)
    before = {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in models}
    original_snapshot = deepcopy(dist.remote_snapshot)
    original_sync = dist.last_sync_at
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        listed = _list(root, users['admin'])
        repeated = _list(root, users['admin'])
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    assert not any(table in sql for sql in statements for table in (
        'usage_facts', 'bill_lines', 'settlement_claims', 'payment_rules', 'bills'))
    ids = [row['id'] for row in listed['items']]
    expected = {row['channel_id']: row for row in order_group_status(db, users['root'], ids)}
    assert listed == repeated
    assert _row(listed, fact.channel_id)['remote_usage_total'] == {
        'amount': '2.5', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert all(row['settlement'] == expected[row['id']] for row in listed['items'])
    assert all('order' not in row['settlement'] and 'financials' not in row['settlement'] for row in listed['items'])
    settled = next(row['settlement'] for row in listed['items'] if row['id'] == fact.channel_id)
    assert settled['status'] == 'settled'
    assert Decimal(settled['usage_amount']) == Decimal(settled['payment_amount']) == 0
    assert settled['order_ids'] == [order['id']]
    after = {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in models}
    assert after == before
    assert _detail(root, order) == original_order
    db.refresh(dist)
    assert dist.remote_snapshot == original_snapshot and dist.last_sync_at == original_sync


def test_remote_total_sums_all_sites_with_their_conversions_including_disabled_site(db, users, ledger, login):
    fact = next(row for row in ledger['facts'] if row.owner_id == users['user'].id
                and row.category_id == ledger['cats']['OpenAI'].id)
    channel = db.get(Channel, fact.channel_id)
    first = db.get(Distribution, fact.distribution_id)
    site = ledger['site']
    site.adapter, site.verified_at = 'tcp-red-v1', STAMP.replace(tzinfo=None)
    site.capabilities = {'verified_version': VERSION, 'usage_conversion': conversion(500000)}
    second_site = Site(name='Disabled total fixture', prefix='TOTAL-DISABLED',
        base_url='https://disabled-total-fixture.invalid', seller_user_id='2',
        token_encrypted=encrypt('fixture-only-disabled-site-token'), enabled=False, adapter='tcp-red-v1',
        verified_at=STAMP.replace(tzinfo=None),
        capabilities={'verified_version': VERSION, 'usage_conversion': conversion(1000000)})
    db.add(second_site)
    db.flush()
    # The same remote id on two different sites denotes two separate targets.
    second = _extra_distribution(db, channel.id, second_site.id, remote_id=first.remote_id)
    for dist, quota in ((first, 1500000), (second, 1250000)):
        dist.status = 'disabled'
        dist.last_sync_at = STAMP.replace(tzinfo=None)
        dist.remote_snapshot = {'id': dist.remote_id, 'status': 2, 'used_quota': quota}
        dist.key_count = 30
    channel.key_count = 50
    db.commit()
    api = login('admin')
    row = _row(_list(api, users['user']), channel.id)
    expected = {'amount': '4.25', 'unit': 'USD', 'covered': 2, 'total': 2}
    assert row['remote_usage_total'] == expected
    assert Decimal(row['settlement']['usage_amount']) == Decimal('4.25')
    assert fact.amount == Decimal(600)
    detail = api.get(f'/api/channels/{channel.id}')
    assert detail.status_code == 200, detail.text
    assert detail.json()['remote_usage_total'] == expected


@pytest.mark.parametrize(('state', 'expected'), [
    ('zero', {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 1}),
    ('missing', {'amount': None, 'unit': 'USD', 'covered': 0, 'total': 1}),
    ('partial', {'amount': '1', 'unit': 'USD', 'covered': 1, 'total': 2}),
    ('zero-and-missing', {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 2}),
    ('no-current-target', {'amount': None, 'unit': 'USD', 'covered': 0, 'total': 0}),
])
def test_remote_total_distinguishes_zero_from_missing_and_partial_coverage(db, users, ledger, login, state, expected):
    fact = next(row for row in ledger['facts'] if row.owner_id == users['user'].id
                and row.category_id == ledger['cats']['OpenAI'].id)
    dist = db.get(Distribution, fact.distribution_id)
    _observe(dist, 500000 if state == 'partial' else 0)
    if state == 'missing':
        dist.remote_snapshot = {'id': dist.remote_id, 'status': 2}
    elif state == 'no-current-target':
        dist.status = 'deleted'
    elif state in ('partial', 'zero-and-missing'):
        _extra_distribution(db, fact.channel_id, dist.site_id)
    db.commit()
    row = _row(_list(login('admin'), users['user']), fact.channel_id)
    assert row['remote_usage_total'] == expected
    assert row['settlement']['status'] == ('unsettled' if state == 'zero' else 'unavailable')
    assert row['settlement']['can_settle'] is (state == 'zero')
    assert fact.amount == Decimal(600)


def test_remote_totals_remain_per_group_and_do_not_mix_pages_or_accounts(db, users, ledger, login):
    foreign, foreign_fact = _add_group(db, ledger, users['other_user'], admin_id=users['other_admin'].id)
    expected = {}
    for index, fact in enumerate(ledger['facts'], start=1):
        _observe(db.get(Distribution, fact.distribution_id), index*500000)
        expected[fact.channel_id] = str(index)
    _observe(db.get(Distribution, foreign_fact.distribution_id), 99000000)
    db.commit()
    root = login('root')
    full = _list(root, users['user'])
    assert full['total'] == 2
    assert foreign.id not in _ids(full)
    for index, original in enumerate(full['items']):
        page = _list(root, users['user'], offset=index, limit=1)
        assert page['total'] == 2 and _ids(page) == {original['id']}
        assert page['items'][0]['remote_usage_total'] == {
            'amount': expected[original['id']], 'unit': 'USD', 'covered': 1, 'total': 1}
    other = _list(root, users['other_user'])
    assert _ids(other) == {foreign.id}
    assert other['items'][0]['remote_usage_total'] == {
        'amount': '198', 'unit': 'USD', 'covered': 1, 'total': 1}
    repeated = _list(root, users['user'])
    assert repeated == full


def test_settled_orders_preserve_remote_total_and_only_new_usage_is_payable(db, users, ledger, login):
    fact = next(row for row in ledger['facts'] if row.owner_id == users['user'].id
                and row.category_id == ledger['cats']['OpenAI'].id)
    _observe(db.get(Distribution, fact.distribution_id), 1250000)
    db.commit()
    api = login('admin')
    before = _row(_list(api, users['user']), fact.channel_id)
    expected = {'amount': '2.5', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert before['remote_usage_total'] == expected and before['settlement']['status'] == 'unsettled'
    order = _create(api, [db.get(Channel, fact.channel_id)])
    original_order = _detail(api, order)
    settled = _row(_list(api, users['user']), fact.channel_id)
    assert settled['remote_usage_total'] == expected and settled['settlement']['status'] == 'settled'
    assert Decimal(settled['settlement']['previous_total']) == Decimal('2.5')
    assert Decimal(settled['settlement']['usage_amount']) == 0
    assert settled['settlement']['can_settle'] is False
    _observe(db.get(Distribution, fact.distribution_id), 1750000)
    db.commit()
    increased = _row(_list(api, users['user']), fact.channel_id)
    assert increased['remote_usage_total']['amount'] == '3.5'
    assert increased['settlement']['status'] == 'unsettled' and increased['settlement']['can_settle'] is True
    assert Decimal(increased['settlement']['usage_amount']) == 1
    assert Decimal(increased['settlement']['payment_amount']) == Decimal('0.7')
    assert _detail(api, order) == original_order
    assert db.scalar(select(func.count(UsageFact.id))) == 4
    assert db.scalar(select(func.count(SettlementOrder.id))) == 1


def test_remote_total_includes_proven_manual_sync_without_exposing_task_snapshot(db, users, ledger, login):
    fact = next(row for row in ledger['facts'] if row.owner_id == users['user'].id
                and row.category_id == ledger['cats']['OpenAI'].id)
    dist = db.get(Distribution, fact.distribution_id)
    task = Task(actor_id=users['admin'].id, owner_id=users['user'].id,
        actor_session_version=users['admin'].session_version, kind='channel_action', status='succeeded')
    db.add(task)
    db.flush()
    sample = remote_distribution(dist.remote_id, quota=1250000, site_id=dist.site_id)
    sample.remote_snapshot['_monitoring']['usage_sync']['task_id'] = task.id
    proof = next(iter(manual_proof(sample).values()))
    dist.status, dist.last_sync_at, dist.remote_snapshot = sample.status, sample.last_sync_at, sample.remote_snapshot
    marker = 'fixture-only-manual-evidence-secret'
    item = TaskItem(task_id=task.id, channel_id=fact.channel_id, site_id=dist.site_id,
        distribution_id=dist.id, operation='sync_usage', status='succeeded', updated_at=proof['updated_at'],
        snapshot={'operation_target': proof['target'], 'operation_result': proof['result'], 'token': marker})
    db.add(item)
    db.commit()
    api = login('admin')
    result = _list(api, users['user'])
    expected = {'amount': '2.5', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert _row(result, fact.channel_id)['remote_usage_total'] == expected
    assert marker not in json.dumps(result)
    detail = api.get(f'/api/channels/{fact.channel_id}')
    assert detail.status_code == 200, detail.text
    assert detail.json()['remote_usage_total'] == expected


@pytest.mark.parametrize(('family', 'definitions'), [
    ('AWS', [
        ('newapi-33-aws-bedrock-v1', 'bedrock', 'AWS Bedrock', 'AWS Bedrock'),
        ('newapi-14-aws-claude-v1', 'aws_claude', 'AWS Claude 代理', 'Claude on AWS'),
    ]),
    ('Azure', [
        ('newapi-3-azure-gpt-v1', 'azure_gpt', 'Azure OpenAI', 'Azure OpenAI'),
        ('newapi-14-azure-claude-v1', 'azure_claude', 'Azure Claude', 'Azure Claude'),
    ]),
])
def test_settlement_service_names_distinguish_formats_and_match_channel_management(
        db, users, service_case, login, family, definitions):
    model_marker = 'fixture-model-metadata-not-for-settlements'
    expected = {}
    for code, variant, name, name_en in definitions:
        channel, _ = service_case.channel(family, code, models=[model_marker])
        expected[channel.id] = {'variant': variant, 'service_name': name, 'service_name_en': name_en}
    db.commit()
    api = login('admin')
    result = _list(api, users['user'])
    assert _ids(result) == set(expected) and result['total'] == 2
    assert model_marker not in json.dumps(result)
    assert 'fixture-private-key' not in json.dumps(result)
    forbidden = {'models', 'format_id', 'format', 'format_name', 'credential_format', 'schema_config',
                 'key_encrypted', 'key_hint', 'fingerprint', 'proxy_encrypted'}
    for row in result['items']:
        assert row['category_id'] == service_case.categories[family].id
        assert {key: row[key] for key in expected[row['id']]} == expected[row['id']]
        assert not forbidden.intersection(row)
        detail = api.get(f"/api/channels/{row['id']}")
        assert detail.status_code == 200, detail.text
        assert {key: detail.json()[key] for key in expected[row['id']]} == expected[row['id']]


def test_settlement_legacy_vertex_type_uses_channel_models_without_returning_them(db, users, service_case, login):
    code = service_case.legacy('vertex_json')
    definitions = [
        (['gemini-2.5-flash'], 'vertex_gemini', 'Vertex AI Gemini', 'Vertex AI Gemini'),
        (['claude-opus-4-6'], 'vertex_claude', 'Vertex AI Claude', 'Vertex AI Claude'),
        (['gemini-2.5-flash', 'claude-opus-4-6'], 'vertex_legacy',
         'Vertex AI（历史接入）', 'Vertex AI (legacy)'),
    ]
    expected = {}
    for models, variant, name, name_en in definitions:
        channel, _ = service_case.channel('Google', code, models=models)
        expected[channel.id] = {'variant': variant, 'service_name': name, 'service_name_en': name_en}
    db.commit()
    api = login('admin')
    result = _list(api, users['user'])
    assert _ids(result) == set(expected) and result['total'] == 3
    encoded = json.dumps(result)
    assert 'gemini-2.5-flash' not in encoded and 'claude-opus-4-6' not in encoded
    assert 'fixture-private-key' not in encoded
    for row in result['items']:
        assert {key: row[key] for key in expected[row['id']]} == expected[row['id']]
        assert not {'models', 'format_id', 'format', 'credential_format', 'schema_config'}.intersection(row)
        detail = api.get(f"/api/channels/{row['id']}")
        assert detail.status_code == 200, detail.text
        assert {key: detail.json()[key] for key in expected[row['id']]} == expected[row['id']]
