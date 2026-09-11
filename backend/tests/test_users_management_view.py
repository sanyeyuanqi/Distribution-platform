"""The management table queries only the data it displays, within the actor's scope."""
import re
from copy import deepcopy

import pytest
from app.db import engine, uid
from app.models import Site
from app.models_channels import Distribution
from sqlalchemy import event, select
from test_settlement_orders import _create, _observe
from test_settlement_orders import order_case as _order_fixture
from test_settlement_orders import service_case as _service_fixture

order_case = _order_fixture
service_case = _service_fixture

PUBLIC_FIELDS = {
    'id', 'display_id', 'username', 'nickname', 'role', 'parent_id', 'active', 'archived', 'created_at',
    'parent_username', 'local_channels', 'remote_channels', 'remote_usage_total',
    'settlement_order_count', 'settlement_order_amount', 'settlement_order_unit',
}


def _users(api, **params):
    response = api.get('/api/users', params={'view': 'management', **params})
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(data):
    return {row['id']: row for row in data['items']}


def _recorded_users(api, **params):
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().lower())

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        data = _users(api, **params)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert not any(sql.startswith(('insert ', 'update ', 'delete ')) for sql in statements)
    return data, statements


def test_management_and_full_alias_only_expose_required_fields_without_historical_queries(db, users, order_case, login):
    _create(login('admin'), [order_case.pair[0]])
    order_case.fact(order_case.pair, '9999')
    order_case.make(owner='admin', quota=2_500_000)
    order_case.make(owner='sibling', quota=1_000_000)
    db.commit()
    api = login('root')
    before = {row.id: deepcopy(row.remote_snapshot) for row in db.scalars(select(Distribution))}
    full, full_queries = _recorded_users(api, view='full')
    compact, compact_queries = _recorded_users(api)
    full_by_id = _by_id(full)
    assert compact['total'] == full['total'] == 5
    for row in compact['items']:
        assert set(row) == PUBLIC_FIELDS
        assert row == {key: value for key, value in full_by_id[row['id']].items() if key in PUBLIC_FIELDS}
    child = _by_id(compact)[users['user'].id]
    assert child['remote_usage_total'] == {'amount': '100', 'unit': 'USD', 'covered': 1, 'total': 1}
    assert (child['settlement_order_count'], child['settlement_order_amount']) == (1, '80')
    assert _by_id(compact)[users['admin'].id]['remote_usage_total']['amount'] == '107'
    # These tables can grow with every usage sync and payment, so their complete
    # absence is stronger evidence than a small difference in HTTP timing.
    forbidden = re.compile(r'\b(?:from|join)\s+(?:usage_facts|bills|payments|categories|settlement_order_lines)\b')
    assert not any(forbidden.search(sql) for sql in compact_queries)
    assert not any(forbidden.search(sql) for sql in full_queries)
    assert len(compact_queries) == len(full_queries)
    assert compact == full
    assert {row.id: row.remote_snapshot for row in db.scalars(
        select(Distribution).execution_options(populate_existing=True))} == before
    assert not any(marker in str(compact) for marker in (
        'fixture-private-key', 'fixture-site-token', 'fixture-order-snapshot-secret',
        'password_hash', 'remote_snapshot', 'token_encrypted'))


def test_management_aggregations_stay_batched_and_selected_account_can_be_loaded_off_page(
        db, users, order_case, login):
    for owner in ('admin', 'sibling', 'other_admin', 'other_user'):
        order_case.make(owner=owner)
    db.commit()
    api = login('root')
    single, single_queries = _recorded_users(api, limit=1)
    all_rows, all_queries = _recorded_users(api, limit=50)
    assert len(all_queries) <= len(single_queries) + 1
    target = all_rows['items'][-1]
    assert target['id'] != single['items'][0]['id']
    selected = _users(api, user_id=target['id'], limit=1)
    assert selected == {'items': [target], 'total': 1}
    assert _users(api, user_id=target['id'], search='no-such-name') == {'items': [], 'total': 0}


@pytest.mark.parametrize('view', ['full', 'management'])
def test_search_matches_public_id_username_and_nickname_and_treats_wildcards_literally(db, users, login, view):
    users['user'].nickname = r'渠道 100%_\Name'
    users['sibling'].nickname = '渠道 100 percent'
    # Identical timestamps must still yield stable page boundaries.
    shared_created_at = users['root'].created_at
    for row in users.values():
        row.created_at = shared_created_at
    db.commit()
    api = login('root')
    default = _users(api, view=view)
    assert [row['id'] for row in default['items']] == sorted(
        (row.id for key, row in users.items() if key != 'root'), reverse=True)
    for search in ('%', '_\\', '\\', '  100%_\\nAME  '):
        result = _users(api, view=view, search=search)
        assert result['total'] == 1 and result['items'][0]['id'] == users['user'].id
    by_username = _users(api, view=view, search='sIbLiNg')
    assert by_username['total'] == 1 and by_username['items'][0]['id'] == users['sibling'].id
    by_id = _users(api, view=view, search=str(users['other_admin'].display_id))
    assert by_id['total'] == 1 and by_id['items'][0]['id'] == users['other_admin'].id
    both = _users(api, view=view, search='渠道')
    assert both['total'] == 2
    pages = [_users(api, view=view, search='渠道', offset=offset, limit=1) for offset in (0, 1)]
    assert [page['items'][0] for page in pages] == both['items']
    assert all(page['total'] == 2 for page in pages)
    assert _users(api, view=view, search='渠道', offset=2, limit=1) == {'items': [], 'total': 2}
    assert _users(api, view=view, search='   ') == default


@pytest.mark.parametrize('view', ['full', 'management'])
def test_account_lookup_search_and_archive_filters_cannot_expand_visibility(db, users, login, view):
    users['sibling'].archived = True
    db.commit()
    admin = login('admin')
    assert set(_by_id(_users(admin, view=view))) == {users['user'].id}
    assert set(_by_id(_users(admin, view=view, include_archived=True))) == {
        users['user'].id, users['sibling'].id}
    for target in ('root', 'admin', 'other_admin', 'other_user', 'sibling'):
        assert _users(admin, view=view, user_id=users[target].id) == {'items': [], 'total': 0}
    assert _users(admin, view=view, user_id=uid()) == {'items': [], 'total': 0}
    assert _users(admin, view=view, search='other') == {'items': [], 'total': 0}
    assert _users(admin, view=view, user_id=users['sibling'].id, include_archived=True)['total'] == 1
    root = login('root')
    assert _users(root, view=view, user_id=users['sibling'].id)['total'] == 0
    assert _users(root, view=view, user_id=users['sibling'].id, include_archived=True)['total'] == 1
    assert login('user').get('/api/users', params={'view': view, 'user_id': users['user'].id}).status_code == 403


def test_management_remote_totals_keep_precision_archive_and_missing_data_rules(db, users, order_case, login):
    channel, dist = order_case.pair
    channel.archived = True
    _observe(dist, 9_007_199_254_740_993)
    order_case.make(quota=1)
    _, deleted = order_case.make(quota=50_000_000)
    deleted.status = 'deleted'
    _, tombstone = order_case.make(quota=50_000_000)
    tombstone.remote_snapshot = {**tombstone.remote_snapshot, '_local_deletion': {'remote_confirmed': False}}
    _, unassigned = order_case.make(quota=50_000_000)
    unassigned.remote_id = None
    _, missing = order_case.make(owner='sibling')
    missing.remote_snapshot = {'id': missing.remote_id, 'status': 3}
    db.commit()
    rows = _by_id(_users(login('root')))
    assert rows[users['user'].id]['remote_usage_total'] == {
        'amount': '18014398509.481988', 'unit': 'USD', 'covered': 2, 'total': 2}
    assert rows[users['admin'].id]['remote_usage_total'] == {
        'amount': '18014398509.481988', 'unit': 'USD', 'covered': 2, 'total': 3}
    assert rows[users['sibling'].id]['remote_usage_total'] == {
        'amount': None, 'unit': 'USD', 'covered': 0, 'total': 1}
    assert rows[users['other_user'].id]['remote_usage_total'] == {
        'amount': '0', 'unit': 'USD', 'covered': 0, 'total': 0}
    # Archived groups contribute lifetime consumption, but not the active local count.
    assert rows[users['user'].id]['local_channels'] == 4


def test_management_remote_identity_is_scoped_to_site_and_not_credential_count(db, users, order_case, login):
    channel, first = order_case.pair
    original_site = db.get(Site, first.site_id)
    other_site = Site(id=uid(), name='Other management site', prefix='management-other',
        base_url='https://management-other.invalid', seller_user_id='1',
        token_encrypted=original_site.token_encrypted, adapter=original_site.adapter,
        verified_at=original_site.verified_at, capabilities=deepcopy(original_site.capabilities))
    db.add(other_site)
    db.flush()
    second = Distribution(id=uid(), channel_id=channel.id, site_id=other_site.id,
        remote_id=first.remote_id, remote_name='matching-number-different-site', key_count=25)
    _observe(second, 12_500_000)
    db.add(second)
    db.commit()
    rows = _by_id(_users(login('root')))
    assert rows[users['user'].id]['remote_usage_total'] == {
        'amount': '125', 'unit': 'USD', 'covered': 2, 'total': 2}


def test_management_order_totals_keep_frozen_payee_and_viewer_payer_scope(db, users, order_case, login):
    child = order_case.pair[0]
    _create(login('admin'), [child])
    _create(login('root'), [child])
    root = login('root')
    before = _by_id(_users(root))
    assert before[users['user'].id]['settlement_order_amount'] == '80'
    assert before[users['admin'].id]['settlement_order_amount'] == '90'
    assert before[users['admin'].id]['settlement_order_count'] == 1
    users['user'].parent_id = users['other_admin'].id
    db.commit()
    after = _by_id(_users(login('other_admin')))
    assert after[users['user'].id]['settlement_order_count'] == 0
    assert after[users['user'].id]['settlement_order_amount'] == '0'
    assert _by_id(_users(root))[users['user'].id]['settlement_order_amount'] == '80'


@pytest.mark.parametrize('params', [
    {'view': 'unknown'}, {'search': 'x' * 201}, {'user_id': 'x' * 65}, {'offset': -1}, {'limit': 501},
])
def test_management_query_validation(login, params):
    assert login('root').get('/api/users', params={'view': 'management', **params}).status_code == 422
