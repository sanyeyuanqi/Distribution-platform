"""Owner-scoped historical destination options and server-side site pagination."""
from types import SimpleNamespace

import pytest
from app.db import uid
from app.models import Site
from app.models_channels import Distribution
from app.security import encrypt
from test_channel_categories import service_case as service_case_fixture

service_case = service_case_fixture
LIST = '/api/channel-distributions'
OPTIONS = '/api/channel-distribution-sites'


def add_site(db, name, *, enabled=True, archived=False):
    row = Site(id=uid(), name=name, prefix=uid()[:8], base_url=f'https://{uid()}.invalid',
               seller_user_id='1', token_encrypted=encrypt('fixture-private-destination-token'),
               enabled=enabled, archived=archived)
    db.add(row)
    db.flush()
    return row


def get_json(client, path, **params):
    result = client.get(path, params=params)
    assert result.status_code == 200, result.text
    assert 'fixture-private-key' not in result.text
    assert 'fixture-private-destination-token' not in result.text
    return result.json()


@pytest.fixture
def destinations(db, service_case):
    live_channel, live_distribution = service_case.channel('Azure', 'newapi-3-azure-gpt-v1')
    active = db.get(Site, live_distribution.site_id)
    active.enabled = True
    disabled = add_site(db, 'Disabled historical destination', enabled=False)
    archived = add_site(db, 'Archived historical destination', enabled=False, archived=True)
    foreign = add_site(db, 'Other team destination')
    unused = add_site(db, 'Destination with no distribution')
    disabled_channel, disabled_distribution = service_case.channel('Azure', 'newapi-14-azure-claude-v1')
    disabled_distribution.site_id = disabled.id
    archived_channel, archived_distribution = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1', archived=True)
    archived_distribution.site_id = archived.id
    archived_distribution.status = 'deleted'
    other_channel, other_distribution = service_case.channel('Azure', 'newapi-3-azure-gpt-v1', owner='other_user')
    other_distribution.site_id = foreign.id
    db.commit()
    return SimpleNamespace(case=service_case, active=active, disabled=disabled, archived=archived,
        foreign=foreign, unused=unused, live=(live_channel, live_distribution),
        disabled_pair=(disabled_channel, disabled_distribution), archived_pair=(archived_channel, archived_distribution),
        foreign_pair=(other_channel, other_distribution))


def test_historical_site_options_include_disabled_archived_and_deleted_distributions(db, destinations, login):
    case = destinations
    client = login('user')
    result = get_json(client, OPTIONS)
    assert result['total'] == 3
    assert {row['id'] for row in result['items']} == {case.active.id, case.disabled.id, case.archived.id}
    assert [row['display_id'] for row in result['items']] == sorted(row['display_id'] for row in result['items'])
    assert all(set(row) == {'id', 'display_id', 'name', 'enabled', 'archived'} for row in result['items'])
    archive = next(row for row in result['items'] if row['id'] == case.archived.id)
    assert archive['enabled'] is False and archive['archived'] is True
    # Ordinary /sites intentionally omits both; historical filtering must not.
    ordinary = get_json(client, '/api/sites')
    assert case.disabled.id not in {row['id'] for row in ordinary['items']}
    assert case.archived.id not in {row['id'] for row in ordinary['items']}
    assert get_json(client, LIST, site_id=case.archived.id) == {'items': [], 'total': 0}
    assert get_json(client, LIST, search='nothing-matches', category_id=case.case.categories['OpenAI'].id) == {
        'items': [], 'total': 0}
    assert get_json(client, OPTIONS) == result
    history = get_json(client, LIST, site_id=case.archived.id, archived=True)
    assert history['total'] == 1 and history['items'][0]['id'] == case.archived_pair[1].id


def test_site_filter_combines_category_variant_search_archive_and_counts_partitions_before_pagination(db, destinations, login):
    case = destinations
    channel, first = case.live
    channel.remark = 'site-filter literal_%'
    partition = Distribution(id=uid(), channel_id=channel.id, site_id=case.active.id,
        partition_key='second', remote_name='second partition')
    another_site = Distribution(id=uid(), channel_id=channel.id, site_id=case.disabled.id,
                               remote_name='other destination')
    db.add_all([partition, another_site])
    case.case.channel('AWS', 'newapi-33-aws-bedrock-v1')
    case.case.channel('Azure', 'newapi-3-azure-gpt-v1', owner='other_user')
    _, archived_same_site = case.case.channel('Azure', 'newapi-3-azure-gpt-v1', archived=True)
    db.commit()
    client = login('user')
    params = {'site_id': case.active.id, 'category_id': case.case.categories['Azure'].id,
              'variant': 'azure_gpt', 'search': 'literal_%', 'limit': 1}
    first_page = get_json(client, LIST, **params)
    second_page = get_json(client, LIST, **params, offset=1)
    assert first_page['total'] == second_page['total'] == 2
    assert {first_page['items'][0]['id'], second_page['items'][0]['id']} == {first.id, partition.id}
    assert get_json(client, LIST, **params, offset=2) == {'items': [], 'total': 2}
    assert all(row['site_id'] == case.active.id for row in first_page['items'] + second_page['items'])
    assert get_json(client, LIST, site_id=case.active.id)['total'] == 3
    archived = get_json(client, LIST, site_id=case.active.id, archived=True)
    assert [row['id'] for row in archived['items']] == [archived_same_site.id]
    assert get_json(client, LIST, site_id=case.active.id, variant='azure_claude')['total'] == 0
    assert get_json(client, LIST, site_id=case.unused.id) == {'items': [], 'total': 0}
    # The existing local-channel endpoint counts matching channels, not partitions.
    local = get_json(client, '/api/channels', site_id=case.active.id,
                     category_id=case.case.categories['Azure'].id, variant='azure_gpt')
    assert local['total'] == 1 and local['items'][0]['id'] == channel.id


def test_owner_scopes_control_both_site_options_and_filter_results(db, users, destinations, login):
    case = destinations
    admin_only = add_site(db, 'Parent-only destination')
    _, admin_distribution = case.case.channel('Azure', 'newapi-3-azure-gpt-v1', owner='admin')
    admin_distribution.site_id = admin_only.id
    db.commit()
    ordinary = login('user')
    assert get_json(ordinary, LIST, site_id=case.foreign.id) == {'items': [], 'total': 0}
    for path in (LIST, OPTIONS):
        assert ordinary.get(path, params={'owner_id': users['other_user'].id}).status_code == 404
    parent = login('admin')
    options = get_json(parent, OPTIONS)
    assert {row['id'] for row in options['items']} == {
        case.active.id, case.disabled.id, case.archived.id, admin_only.id}
    scoped = get_json(parent, OPTIONS, owner_id=users['user'].id)
    assert {row['id'] for row in scoped['items']} == {case.active.id, case.disabled.id, case.archived.id}
    assert get_json(parent, LIST, site_id=case.foreign.id)['total'] == 0
    parent_only = get_json(parent, LIST, site_id=admin_only.id)
    assert parent_only['total'] == 1 and parent_only['items'][0]['id'] == admin_distribution.id
    root = login('root')
    assert case.foreign.id in {row['id'] for row in get_json(root, OPTIONS)['items']}
    assert case.unused.id not in {row['id'] for row in get_json(root, OPTIONS)['items']}
    assert get_json(root, LIST, site_id=case.foreign.id)['total'] == 1
    assert get_json(ordinary, OPTIONS, owner_id=users['user'].id) == scoped


def test_site_options_are_distinct_for_multiple_partitions_and_empty_for_no_owned_history(db, destinations, login):
    case = destinations
    db.add(Distribution(channel_id=case.live[0].id, site_id=case.active.id,
                        partition_key='duplicate-site-option', remote_name='another partition'))
    db.commit()
    result = get_json(login('user'), OPTIONS)
    assert result['total'] == 3
    assert sum(row['id'] == case.active.id for row in result['items']) == 1
    assert get_json(login('sibling'), OPTIONS) == {'items': [], 'total': 0}


def test_site_options_require_authentication(client):
    assert client.get(OPTIONS).status_code == 401
