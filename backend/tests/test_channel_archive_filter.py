"""Archive filters share authorized channel scope across lists, categories, and export."""
# ruff: noqa: F811
import csv
import io
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.db import uid
from app.models_channels import Distribution
from test_channel_categories import service_case  # noqa: F401

PATHS = ('/api/channels', '/api/channel-distributions', '/api/channel-categories', '/api/usage/export')
OWNERS = ('root', 'admin', 'other_admin', 'user', 'sibling', 'other_user')


@pytest.fixture
def archive_filter_case(db, service_case):
    pairs = {}
    amounts = {}
    for index, owner in enumerate(OWNERS, 1):
        for archived in (False, True):
            pair = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner=owner, archived=archived)
            amount = Decimal(index + 10 * archived)
            service_case.fact(pair, str(amount))
            pairs[(owner, archived)] = pair
            amounts[pair[0].id] = amount
    active = pairs[('user', False)]
    extra = Distribution(id=uid(), channel_id=active[0].id, site_id=active[1].site_id,
                         partition_key='second', remote_name='second partition', status='enabled')
    db.add(extra)
    # Other services/categories must remain excluded when the archive filter is all.
    for family, code in (('AWS', 'newapi-14-aws-claude-v1'), ('Azure', 'newapi-3-azure-gpt-v1')):
        service_case.fact(service_case.channel(family, code, archived=True), '100')
    # Both owners belong to the administrator, but this fact has the wrong channel owner.
    mismatch = service_case.fact(active, '999', owner='sibling')
    db.commit()
    return SimpleNamespace(pairs=pairs, amounts=amounts, extra=extra, mismatch=mismatch,
                           category_id=service_case.categories['AWS'].id)


def export_records(response):
    assert response.status_code == 200, response.text
    return list(csv.reader(io.StringIO(response.text.lstrip('\ufeff'))))[1:]


def check_views(client, case, owners, archived, *, owner_id=None):
    params = {'category_id': case.category_id, 'variant': 'bedrock'}
    if archived is not None:
        params['archived'] = archived
    if owner_id:
        params['owner_id'] = owner_id
    states = (False, True) if archived == 'all' else (archived == 'true',)
    expected_pairs = [pair for (owner, state), pair in case.pairs.items() if owner in owners and state in states]
    channel_ids = {pair[0].id for pair in expected_pairs}
    distribution_ids = {pair[1].id for pair in expected_pairs}
    if case.extra.channel_id in channel_ids:
        distribution_ids.add(case.extra.id)
    for path, expected_ids, nested in ((PATHS[0], channel_ids, False), (PATHS[1], distribution_ids, True)):
        items = []
        # Page through the mixed result so filtering and total counts must precede pagination.
        for offset in range(0, len(expected_ids) + 1, 2):
            response = client.get(path, params={**params, 'offset': offset, 'limit': 2})
            assert response.status_code == 200, response.text
            assert 'fixture-private-key' not in response.text and 'fixture-site-token' not in response.text
            page = response.json()
            assert page['total'] == len(expected_ids) and len(page['items']) <= 2
            items.extend(page['items'])
        assert len(items) == len(expected_ids)
        assert {row['id'] for row in items} == expected_ids
        assert {(row['channel'] if nested else row)['archived'] for row in items} == set(states)
        assert {row['channel_id'] if nested else row['id'] for row in items} == channel_ids
    response = client.get(PATHS[2], params=params)
    assert response.status_code == 200, response.text
    option = next(row for row in response.json()['items']
                  if row['category_id'] == case.category_id and row['variant'] == 'bedrock')
    expected_total = sum((case.amounts[channel_id] for channel_id in channel_ids), Decimal(0))
    assert option['channel_count'] == len(channel_ids)
    assert Decimal(option['verified_usage_by_unit']['USD']) == expected_total
    records = export_records(client.get(PATHS[3], params=params))
    # Export historically includes both states when archived is omitted.
    export_ids = ({pair[0].id for (owner, _), pair in case.pairs.items() if owner in owners}
                  if archived is None else channel_ids)
    assert {row[2] for row in records} == export_ids
    assert sum((Decimal(row[8]) for row in records), Decimal(0)) == sum(
        (case.amounts[channel_id] for channel_id in export_ids), Decimal(0))


@pytest.mark.parametrize('archived', [None, 'false', 'true', 'all'])
def test_archive_states_pagination_category_counts_and_export(db, archive_filter_case, login, archived):
    check_views(login('user'), archive_filter_case, {'user'}, archived)


@pytest.mark.parametrize(('role', 'owners', 'target', 'forbidden'), [
    ('root', set(OWNERS), 'other_user', None),
    ('admin', {'admin', 'user', 'sibling'}, 'user', 'other_user'),
    ('user', {'user'}, 'user', 'sibling'),
])
def test_all_preserves_role_scope_and_owner_drilldown(archive_filter_case, login, users,
                                                     role, owners, target, forbidden):
    client = login(role)
    check_views(client, archive_filter_case, owners, 'all')
    check_views(client, archive_filter_case, {target}, 'all', owner_id=users[target].id)
    for path in PATHS:
        owner_id = users[forbidden].id if forbidden else 'nonexistent'
        assert client.get(path, params={'archived': 'all', 'owner_id': owner_id}).status_code == 404


def test_export_all_without_variant_keeps_channel_owner_consistency_and_legacy_default(
        archive_filter_case, login):
    client = login('admin')
    explicit = export_records(client.get(PATHS[3], params={'archived': 'all'}))
    historical = export_records(client.get(PATHS[3]))
    assert archive_filter_case.mismatch.id not in {row[0] for row in explicit}
    assert {row[0] for row in historical} == {row[0] for row in explicit} | {archive_filter_case.mismatch.id}
    channels = client.get(PATHS[0], params={'archived': 'all'}).json()['items']
    assert {row[2] for row in explicit} == {row['id'] for row in channels}


@pytest.mark.parametrize('path', PATHS)
def test_invalid_archive_state_is_rejected(login, path):
    assert login('user').get(path, params={'archived': 'invalid'}).status_code == 422
