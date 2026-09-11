"""Channel views and export always use the actor's full authorized ownership scope."""
# ruff: noqa: F811
import csv
import io
from decimal import Decimal

import pytest
from app.main import app
from test_channel_categories import service_case  # noqa: F401

PATHS = ('/api/channels', '/api/channel-categories', '/api/usage/export')


@pytest.fixture
def fixed_scope_case(db, service_case):
    pairs = {}
    for index, owner in enumerate(('root', 'admin', 'other_admin', 'user', 'sibling', 'other_user'), 1):
        pair = service_case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner=owner)
        service_case.fact(pair, str(index))
        pairs[owner] = (pair[0], Decimal(index))
    db.commit()
    return service_case.categories['AWS'].id, pairs


def assert_views(client, case, allowed, *, extra=None):
    category_id, pairs = case
    params = {'category_id': category_id, 'variant': 'bedrock', 'archived': 'false', **(extra or {})}
    channels = client.get(PATHS[0], params=params)
    categories = client.get(PATHS[1], params=params)
    exported = client.get(PATHS[2], params=params)
    for response in (channels, categories, exported):
        assert response.status_code == 200, response.text
    channel_ids = {pairs[owner][0].id for owner in allowed}
    assert {row['id'] for row in channels.json()['items']} == channel_ids
    assert channels.json()['total'] == len(allowed)
    option = next(row for row in categories.json()['items']
                  if row['category_id'] == category_id and row['variant'] == 'bedrock')
    assert option['channel_count'] == len(allowed)
    expected = sum((pairs[owner][1] for owner in allowed), Decimal(0))
    assert Decimal(option['verified_usage_by_unit'].get('USD', '0')) == expected
    records = list(csv.reader(io.StringIO(exported.text.lstrip('\ufeff'))))[1:]
    assert {row[2] for row in records} == channel_ids
    assert sum((Decimal(row[8]) for row in records), Decimal(0)) == expected
    assert not any('fixture-private-key' in response.text for response in (channels, categories, exported))


@pytest.mark.parametrize(('role', 'allowed'), [
    ('root', {'root', 'admin', 'other_admin', 'user', 'sibling', 'other_user'}),
    ('admin', {'admin', 'user', 'sibling'}),
    ('user', {'user'}),
])
def test_fixed_role_scope_and_obsolete_query_cannot_narrow_or_expand(login, fixed_scope_case, role, allowed):
    client = login(role)
    assert_views(client, fixed_scope_case, allowed)
    for old_value in ('mine', 'team', 'invalid'):
        assert_views(client, fixed_scope_case, allowed, extra={'scope': old_value})


@pytest.mark.parametrize(('role', 'owner', 'forbidden'), [
    ('root', 'other_user', None), ('admin', 'user', 'other_user'), ('user', 'user', 'sibling'),
])
def test_owner_drilldown_keeps_exact_scope_and_unauthorized_targets_return_404(
        login, users, fixed_scope_case, role, owner, forbidden):
    client = login(role)
    assert_views(client, fixed_scope_case, {owner}, extra={'owner_id': users[owner].id})
    for target in ('does-not-exist', users[forbidden].id if forbidden else 'another-nonexistent-user'):
        for path in PATHS:
            assert client.get(path, params={'owner_id': target, 'scope': 'mine'}).status_code == 404


def test_removed_scope_is_not_in_openapi_and_other_statistics_contracts_stay_intact():
    paths = app.openapi()['paths']
    for path in PATHS:
        names = {item['name'] for item in paths[path]['get'].get('parameters', [])}
        assert 'scope' not in names and 'owner_id' in names
    for path in ('/api/usage', '/api/dashboard', '/api/tasks'):
        assert 'scope' in {item['name'] for item in paths[path]['get'].get('parameters', [])}


def test_fixed_scope_endpoints_still_require_authentication(client):
    for path in PATHS:
        assert client.get(path).status_code == 401
