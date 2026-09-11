"""Lightweight uploader choices and scoped literal search on both channel lists."""
from types import SimpleNamespace

import pytest
from app.db import uid
from app.models import Site
from app.models_channels import Distribution, UploadGroup
from app.security import encrypt
from sqlalchemy import event
from test_channel_categories import service_case as service_case_fixture

service_case = service_case_fixture
OPTIONS = '/api/channel-uploaders'
LISTS = ('/api/channels', '/api/channel-distributions')
GPT = 'newapi-3-azure-gpt-v1'


def read(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert 'fixture-private-key' not in response.text and 'fixture-site-token' not in response.text
    return response.json()


@pytest.fixture
def uploaders(db, users, service_case):
    users['user'].username = 'member.alpha'
    users['user'].nickname = '上传者甲 literal_%\\unit'
    users['other_user'].username = 'memberXalpha'
    users['other_user'].nickname = 'foreign-only-uploader literalAZunit'
    main = [service_case.channel('Azure', GPT, models=['shared-model']) for _ in range(2)]
    alternate = service_case.channel('Azure', 'newapi-14-azure-claude-v1')
    archived = service_case.channel('Azure', GPT, archived=True)
    sibling = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner='sibling', archived=True)
    users['sibling'].active = False
    users['sibling'].archived = True
    admin = service_case.channel('Azure', GPT, owner='admin')
    foreign = service_case.channel('Azure', GPT, owner='other_user')
    site = Site(name='Second uploader fixture site', prefix=uid()[:8], base_url='https://second-uploader.invalid',
                seller_user_id='1', token_encrypted=encrypt('fixture-site-token'))
    db.add(site)
    db.flush()
    partition = Distribution(channel_id=main[0][0].id, site_id=main[0][1].site_id,
                             partition_key='another-partition', remote_name='partition')
    other_site = Distribution(channel_id=main[0][0].id, site_id=site.id, remote_name='other-site')
    db.add_all([partition, other_site])
    db.commit()
    return SimpleNamespace(main=main, alternate=alternate, archived=archived, sibling=sibling, admin=admin,
                           foreign=foreign, partition=partition, other_site=other_site, case=service_case)


def test_choices_are_distinct_authorized_historical_and_independent_of_list_filters(db, users, login, uploaders):
    clients = {role: login(role) for role in ('root', 'admin', 'user')}
    expected = {'root': {'admin', 'user', 'sibling', 'other_user'},
                'admin': {'admin', 'user', 'sibling'}, 'user': {'user'}}
    for role, client in clients.items():
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany, statements=statements):
            statements.append(statement)

        event.listen(db.bind, 'before_cursor_execute', capture)
        try:
            result = read(client, OPTIONS)
        finally:
            event.remove(db.bind, 'before_cursor_execute', capture)
        assert result['total'] == len(expected[role])
        assert {row['id'] for row in result['items']} == {users[owner].id for owner in expected[role]}
        assert all(set(row) == {'id', 'username', 'nickname'} for row in result['items'])
        assert [row['id'] for row in result['items']] == [
            users[owner].id for owner in sorted(expected[role], key=lambda key: users[key].display_id)]
        assert not any(table in statement for statement in statements
                       for table in ('usage_facts', 'bill_lines', 'bills', 'channels.key_encrypted'))
        assert read(client, OPTIONS, owner_id=users['other_user'].id, category_id='not-selected',
                    search='nothing-matches', archived='false', site_id='none') == result
    assert read(clients['admin'], '/api/channels', owner_id=users['sibling'].id)['total'] == 0
    assert read(clients['admin'], '/api/channels', owner_id=users['sibling'].id, archived=True)['total'] == 1


def test_uploader_options_require_login_and_empty_history_is_empty(client, login):
    assert client.get(OPTIONS).status_code == 401
    assert read(login('user'), OPTIONS) == {'items': [], 'total': 0}


@pytest.mark.parametrize('path', LISTS)
def test_uploader_search_matches_literal_username_nickname_and_does_not_expand_owner_scope(
        db, users, login, uploaders, path):
    case = uploaders
    root, parent, member = login('root'), login('admin'), login('user')
    expected = ({pair[0].id for pair in [*case.main, case.alternate]} if path == '/api/channels' else
                {pair[1].id for pair in [*case.main, case.alternate]} | {case.partition.id, case.other_site.id})
    for term in ('MEMBER.ALPHA', '上传者甲', 'literal_%', '\\unit', 'literal_%\\unit'):
        result = read(root, path, search=term)
        assert {row['id'] for row in result['items']} == expected
        assert result['total'] == len(expected)
    foreign_term = users['other_user'].nickname
    for client in (parent, member):
        assert read(client, path, search=foreign_term) == {'items': [], 'total': 0}
        assert client.get(path, params={'owner_id': users['other_user'].id, 'search': foreign_term}).status_code == 404
    assert read(root, path, search=foreign_term)['total'] == 1
    assert read(root, path, owner_id=users['user'].id, search=foreign_term) == {'items': [], 'total': 0}


@pytest.mark.parametrize('path', LISTS)
def test_uploader_filter_search_service_site_archive_and_pagination_remain_composable(
        db, users, login, uploaders, path):
    case = uploaders
    case.main[1][0].created_at = case.main[0][0].created_at
    db.commit()
    client = login('admin')
    params = {'owner_id': users['user'].id, 'search': '上传者甲',
              'category_id': case.case.categories['Azure'].id, 'variant': 'azure_gpt',
              'site_id': case.main[0][1].site_id, 'archived': False, 'limit': 1}
    expected = ({pair[0].id for pair in case.main} if path == '/api/channels' else
                {pair[1].id for pair in case.main} | {case.partition.id})
    pages = [read(client, path, **params, offset=index) for index in range(len(expected))]
    assert all(page['total'] == len(expected) and len(page['items']) == 1 for page in pages)
    assert {page['items'][0]['id'] for page in pages} == expected
    if path == '/api/channels':
        assert [page['items'][0]['id'] for page in pages] == sorted(expected, reverse=True)
    assert read(client, path, **params, offset=len(expected)) == {'items': [], 'total': len(expected)}
    history = read(client, path, **{**params, 'archived': True})
    assert history['total'] == 1
    assert history['items'][0]['id'] == case.archived[0 if path == '/api/channels' else 1].id
    assert read(client, path, **{**params, 'owner_id': users['admin'].id}) == {'items': [], 'total': 0}
    if path == '/api/channels':
        specific = read(client, path, **params, group_id=case.main[0][0].group_id,
                        model='shared-model', status='enabled')
        assert specific['total'] == 1 and specific['items'][0]['id'] == case.main[0][0].id


def test_group_tag_and_name_search_match_both_lists_without_returning_unrelated_channels(db, login, uploaders):
    case = uploaders
    group = db.get(UploadGroup, case.main[0][0].group_id)
    group.tag, group.name = 'upload-tag-literal_%\\suffix', 'Unique uploader batch name'
    db.commit()
    client = login('user')
    for path in LISTS:
        expected = ({case.main[0][0].id} if path == '/api/channels' else
                    {case.main[0][1].id, case.partition.id, case.other_site.id})
        for term in (group.tag, group.name):
            result = read(client, path, search=term)
            assert result['total'] == len(expected)
            assert {row['id'] for row in result['items']} == expected
