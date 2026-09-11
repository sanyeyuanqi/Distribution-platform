"""Flat distribution pages retain authorized filters, independent status, and safe amounts."""
# ruff: noqa: F811
from datetime import UTC, datetime

import pytest
from app.db import engine, uid
from app.models import Site
from app.models_channels import Distribution, Task, TaskItem, UploadGroup
from app.security import encrypt
from sqlalchemy import event
from test_channel_categories import service_case  # noqa: F401
from test_channel_category_remote_usage import observe

PATH = '/api/channel-distributions'
CODE = 'newapi-33-aws-bedrock-v1'


def listing(client, **params):
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    assert 'fixture-private-key' not in response.text and 'fixture-site-token' not in response.text
    return response.json()


def test_real_distribution_pagination_counts_same_channel_site_partitions_and_has_stable_ties(db, service_case, login):
    channel, first = service_case.channel('AWS', CODE)
    rows = [first]
    for index in range(1, 53):
        row = Distribution(id=uid(), channel_id=channel.id, site_id=first.site_id,
                           partition_key=f'part-{index}', partition_label=f'分区 {index}',
                           remote_name=f'partition-{index}', status='disabled', key_count=2)
        db.add(row)
        db.flush()
        rows.append(row)
    for row in rows:
        observe(db, (channel, row), 0)
        row.created_at = datetime(2026, 9, 10, 1, tzinfo=UTC).replace(tzinfo=None)
    db.commit()
    client = login('user')
    first_page = listing(client)
    second_page = listing(client, offset=50)
    assert first_page['total'] == second_page['total'] == 53
    assert len(first_page['items']) == 50 and len(second_page['items']) == 3
    all_items = first_page['items'] + second_page['items']
    assert [row['id'] for row in all_items] == sorted((row.id for row in rows), reverse=True)
    for row in all_items:
        assert row['channel_id'] == channel.id and row['channel']['id'] == channel.id
        assert row['remote_usage_total'] == {'amount': '0', 'unit': 'USD', 'covered': 1, 'total': 1}
        assert row['channel']['variant'] == 'bedrock'
        assert 'distributions' not in row and 'distributions' not in row['channel']
        assert 'key_encrypted' not in row['channel'] and 'key_hint' not in row['channel']
    assert listing(client, offset=1000) == {'items': [], 'total': 53}
    assert listing(client, limit=0, offset=-1)['items'][0]['id'] == first_page['items'][0]['id']


@pytest.mark.parametrize(('role', 'owners'), [
    ('root', {'root', 'admin', 'user', 'other_user'}),
    ('admin', {'admin', 'user'}), ('user', {'user'}),
])
def test_fixed_authorization_owner_drilldown_archived_and_service_variant(db, users, service_case, login, role, owners):
    case = service_case
    pairs = {owner: case.channel('Azure', 'newapi-3-azure-gpt-v1', owner=owner)
             for owner in ('root', 'admin', 'user', 'other_user')}
    alternate = case.channel('Azure', 'newapi-14-azure-claude-v1')
    archived = case.channel('Azure', 'newapi-3-azure-gpt-v1', archived=True)
    case.channel('AWS', CODE)
    db.commit()
    client = login(role)
    params = {'category_id': case.categories['Azure'].id, 'variant': 'azure_gpt'}
    result = listing(client, **params)
    assert {row['id'] for row in result['items']} == {pairs[owner][1].id for owner in owners}
    assert result['total'] == len(owners)
    assert {row['channel']['owner_id'] for row in result['items']} == {users[owner].id for owner in owners}
    mine = listing(client, **params, owner_id=users['user'].id)
    assert [row['id'] for row in mine['items']] == [pairs['user'][1].id]
    archived_result = listing(client, **params, archived=True)
    assert [row['id'] for row in archived_result['items']] == [archived[1].id]
    other_service = listing(client, category_id=params['category_id'], variant='azure_claude')
    assert [row['id'] for row in other_service['items']] == [alternate[1].id]
    assert other_service['items'][0]['channel']['service_name'] == 'Azure Claude'
    for query in ({'owner_id': 'nonexistent'}, {'owner_id': users['other_user'].id} if role != 'root' else {'owner_id': 'none'}):
        assert client.get(PATH, params=query).status_code == 404


def test_search_matches_group_channel_and_exact_site_remote_fields_without_expanding_other_rows(db, service_case, login):
    case = service_case
    channel, first = case.channel('AWS', CODE)
    channel.remark = 'only-note literal_%'
    group = db.get(UploadGroup, channel.group_id)
    group.tag, group.name = 'only-label-tag', 'only-label-title'
    site = Site(name='only-second-site', prefix='Second', base_url='https://second.invalid',
                seller_user_id='1', token_encrypted=encrypt('fixture-site-token'))
    db.add(site)
    db.flush()
    second = Distribution(channel_id=channel.id, site_id=site.id, remote_name='only-remote-title', remote_id='9876543210')
    db.add(second)
    case.channel('AWS', CODE)
    db.commit()
    client = login('user')
    both = {first.id, second.id}
    for search in (channel.id, 'only-note', 'literal_%', group.tag, group.name):
        assert {row['id'] for row in listing(client, search=search)['items']} == both
    for search in (site.name, second.remote_name, second.remote_id):
        found = listing(client, search=search)
        assert found['total'] == 1 and found['items'][0]['id'] == second.id
    assert listing(client, search='missing-search') == {'items': [], 'total': 0}


def test_upload_remote_state_and_single_distribution_amount_use_existing_projections(db, service_case, login):
    case = service_case
    failed = case.channel('AWS', CODE)
    pending = case.channel('AWS', CODE)
    existing = case.channel('AWS', CODE)
    remote = observe(db, existing, 1250000, manual=True)
    remote.error = '后续操作失败，不是上传失败'
    case.fact(existing, '99')
    failed[1].status, pending[1].status = 'failed', 'pending'
    task = Task(id=uid(), actor_id=failed[0].owner_id, owner_id=failed[0].owner_id,
                actor_session_version=1, kind='create', status='failed')
    db.add(task)
    db.flush()
    for pair, status, error in ((failed, 'failed', '渠道分组过长'), (pending, 'pending', None)):
        db.add(TaskItem(task_id=task.id, channel_id=pair[0].id, distribution_id=pair[1].id,
            site_id=pair[1].site_id, operation='create', status=status, error=error))
    db.commit()
    client = login('user')
    items = {row['id']: row for row in listing(client)['items']}
    assert items[failed[1].id]['upload_status'] == 'failed'
    assert items[failed[1].id]['upload_message'] == '渠道分组过长'
    assert items[pending[1].id]['upload_status'] == 'pending'
    assert items[failed[1].id]['remote_status'] == items[pending[1].id]['remote_status'] == 'unavailable'
    assert items[failed[1].id]['remote_usage_total']['amount'] is None
    confirmed = items[remote.id]
    assert confirmed['upload_status'] == 'succeeded' and confirmed['remote_status'] == 'disabled'
    assert confirmed['remote_disable_reason'] == 'automatic'
    assert confirmed['remote_usage_total']['amount'] == '2.5'
    assert confirmed['verified_usage_by_unit'] == {'USD': '99.00000000'}
    detail = client.get('/api/channels/' + existing[0].id).json()['distributions'][0]
    for field in ('upload_status', 'upload_message', 'remote_status', 'remote_disable_reason', 'usage_sync'):
        assert confirmed[field] == detail[field]


def test_page_queries_are_bounded_and_do_not_select_channel_keys(db, service_case, login):
    for _ in range(20):
        observe(db, service_case.channel('AWS', CODE), 500000, manual=True)
    db.commit()
    client = login('user')

    def measured(limit):
        statements = []

        def record(*args):
            statements.append(args[2])

        event.listen(engine, 'before_cursor_execute', record)
        try:
            result = listing(client, limit=limit)
        finally:
            event.remove(engine, 'before_cursor_execute', record)
        assert len(result['items']) == limit and result['total'] == 20
        assert all(row['remote_usage_total']['amount'] == '1' for row in result['items'])
        assert not any('channels.key_encrypted' in sql or 'channels.fingerprint' in sql for sql in statements)
        return len(statements)

    assert measured(20) <= measured(1) + 1


def test_distribution_list_requires_login(client):
    assert client.get(PATH).status_code == 401
