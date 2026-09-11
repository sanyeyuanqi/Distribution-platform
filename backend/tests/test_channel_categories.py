"""Authorized service filters and fact totals, using only the isolated test DB."""
import csv
import io
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.bootstrap import seed_catalog
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_billing import UsageFact
from app.models_channels import Channel, Distribution, UploadGroup
from app.security import encrypt
from sqlalchemy import select


@pytest.fixture
def service_case(db, users):
    seed_catalog(db)
    categories = {row.family: row for row in db.scalars(select(Category))}
    formats = {(row.category_id, row.code): row for row in db.scalars(select(CredentialFormat))}
    site = Site(id=uid(), name='Service fixture', prefix='svc', base_url='https://service.invalid',
                seller_user_id='1', token_encrypted=encrypt('fixture-site-token'))
    db.add(site)
    db.flush()

    def channel(family, code, *, owner='user', archived=False, models=None):
        category = categories[family]
        fmt = formats[(category.id, code)]
        group = UploadGroup(id=uid(), owner_id=users[owner].id, category_id=category.id,
                            format_id=fmt.id, tag='fixture-' + uid())
        db.add(group)
        db.flush()
        row = Channel(id=uid(), owner_id=users[owner].id, category_id=category.id, format_id=fmt.id,
                      group_id=group.id, key_encrypted=encrypt('fixture-private-key'), key_hint='masked',
                      fingerprint=uid(), models=models or [], archived=archived)
        db.add(row)
        db.flush()
        dist = Distribution(id=uid(), channel_id=row.id, site_id=site.id, remote_name='fixture',
                            status='enabled', models=row.models)
        db.add(dist)
        db.flush()
        return row, dist

    def fact(pair, amount='1', *, unit='USD', verified=True, owner=None):
        row, dist = pair
        value = Decimal(amount) if amount is not None else None
        item = UsageFact(id=uid(), source_id=uid(), channel_id=row.id, distribution_id=dist.id,
                         site_id=site.id, owner_id=users[owner].id if owner else row.owner_id,
                         admin_id=users['admin'].id, category_id=row.category_id, occurred_at=utcnow(),
                         raw_amount=value if value is not None else Decimal(1), raw_unit=unit, amount=value, unit=unit,
                         verified=verified, conversion_version='fixture-v1')
        db.add(item)
        db.flush()
        return item

    def legacy(kind):
        category = categories['Google']
        code = f'newapi-41-{kind.replace("_", "-")}-v1'
        fmt = CredentialFormat(id=uid(), category_id=category.id, code=code, name='Old Vertex',
                               version='1', schema_config={'type': kind, 'remote_type': 41})
        db.add(fmt)
        db.flush()
        formats[(category.id, code)] = fmt
        return code

    db.commit()
    return SimpleNamespace(categories=categories, channel=channel, fact=fact, legacy=legacy)


def menu(client, **params):
    response = client.get('/api/channel-categories', params=params)
    assert response.status_code == 200, response.text
    return {(row['category_id'], row['variant']): row for row in response.json()['items']}


def test_empty_catalog_has_distinct_business_services(service_case, login):
    case = service_case
    result = menu(login('user'))
    expected = {'AWS': ['bedrock', 'aws_claude'], 'Azure': ['azure_gpt', 'azure_claude'],
                'Google': ['ai_studio_gemini', 'vertex_gemini', 'vertex_claude'],
                'Anthropic': [''], 'OpenAI': [''], 'OpenRouter': [''], 'OpenCode': ['']}
    assert set(result) == {(case.categories[family].id, variant)
                           for family, variants in expected.items() for variant in variants}
    for row in result.values():
        assert row['channel_count'] == 0
        assert row['data_status'] == 'empty'
        assert row['name'] and row['name_en']
    assert result[(case.categories['AWS'].id, 'aws_claude')]['name_en'] == 'Claude on AWS'


def test_totals_cover_all_pages_and_isolate_units_owners_and_archives(db, users, service_case, login):
    case = service_case
    for _ in range(53):
        pair = case.channel('AWS', 'newapi-33-aws-bedrock-v1')
        case.fact(pair, '0.10')
    case.fact(pair, '7', unit='EUR')
    case.fact(pair, '999', verified=False)
    case.fact(pair, None)
    case.fact(pair, '888', owner='other_user')
    case.fact(pair, '777', owner='sibling')
    case.fact(case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner='sibling'), '2')
    case.fact(case.channel('AWS', 'newapi-33-aws-bedrock-v1', owner='other_user'), '12345')
    case.fact(case.channel('AWS', 'newapi-33-aws-bedrock-v1', archived=True), '44')
    db.commit()
    client = login('user')
    key = (case.categories['AWS'].id, 'bedrock')
    row = menu(client)[key]
    assert row['channel_count'] == 53
    assert {unit: Decimal(value) for unit, value in row['verified_usage_by_unit'].items()} == {
        'USD': Decimal('5.3'), 'EUR': Decimal(7)}
    assert row['data_status'] == 'partial'
    listing = client.get('/api/channels', params={'category_id': key[0], 'variant': key[1], 'limit': 50}).json()
    assert listing['total'] == 53 and len(listing['items']) == 50
    archived = menu(client, archived='true')[key]
    assert archived['channel_count'] == 1 and Decimal(archived['verified_usage_by_unit']['USD']) == 44
    team = menu(login('admin'))[key]
    assert team['channel_count'] == 54 and Decimal(team['verified_usage_by_unit']['USD']) == Decimal('7.3')
    mine = menu(login('admin'), owner_id=users['admin'].id)[key]
    assert mine['channel_count'] == 0
    assert menu(login('admin'), owner_id=users['user'].id)[key]['channel_count'] == 53
    assert client.get('/api/channel-categories', params={'owner_id': users['other_user'].id}).status_code == 404
    assert 'fixture-private-key' not in str(row)


@pytest.mark.parametrize(('family', 'definitions'), [
    ('AWS', [('newapi-33-aws-bedrock-v1', 'bedrock'), ('newapi-33-aws-ak-sk-v1', 'bedrock'),
             ('newapi-33-aws-api-key-v1', 'bedrock'), ('newapi-14-aws-claude-v1', 'aws_claude')]),
    ('Azure', [('newapi-3-azure-gpt-v1', 'azure_gpt'), ('newapi-14-azure-claude-v1', 'azure_claude')]),
    ('Google', [('newapi-24-api-key-v1', 'ai_studio_gemini'),
                ('newapi-41-vertex-gemini-v1', 'vertex_gemini'), ('newapi-41-vertex-claude-v1', 'vertex_claude')]),
])
def test_service_filters_align_channels_and_usage_export(db, service_case, login, family, definitions):
    case = service_case
    expected = {}
    for code, variant in definitions:
        pair = case.channel(family, code)
        case.fact(pair, '1.25')
        expected.setdefault(variant, []).append(pair[0])
    db.commit()
    client = login('user')
    category_id = case.categories[family].id
    for variant, matching in expected.items():
        params = {'category_id': category_id, 'variant': variant, 'archived': 'false'}
        response = client.get('/api/channels', params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert {row['id'] for row in result['items']} == {row.id for row in matching}
        for row in result['items']:
            assert row['variant'] == variant and row['service_name'] and row['service_name_en']
        exported = client.get('/api/usage/export', params=params)
        assert exported.status_code == 200, exported.text
        data = list(csv.reader(io.StringIO(exported.text.lstrip('\ufeff'))))
        assert {row[2] for row in data[1:]} == {row.id for row in matching}
    assert client.get('/api/channels', params={'category_id': category_id}).json()['total'] == len(definitions)


def test_legacy_vertex_classification_uses_channel_model_scope(db, service_case, login):
    case = service_case
    json_code = case.legacy('vertex_json')
    api_code = case.legacy('vertex_api_key')
    gemini = case.channel('Google', json_code, models=['gemini-2.5-flash'])[0]
    claude = case.channel('Google', json_code, models=['claude-opus-4-6'])[0]
    mixed = case.channel('Google', json_code, models=['gemini-2.5-flash', 'claude-opus-4-6'])[0]
    unknown = case.channel('Google', api_code, models=['claude-opus-4-6'])[0]
    db.commit()
    client = login('user')
    result = menu(client)
    category = case.categories['Google'].id
    expected = {'vertex_gemini': {gemini.id}, 'vertex_claude': {claude.id}, 'vertex_legacy': {mixed.id, unknown.id}}
    for variant, ids in expected.items():
        assert result[(category, variant)]['channel_count'] == len(ids)
        rows = client.get('/api/channels', params={'category_id': category, 'variant': variant}).json()['items']
        assert {row['id'] for row in rows} == ids


def test_missing_usage_does_not_become_verified_zero(db, service_case, login):
    case = service_case
    case.channel('AWS', 'newapi-14-aws-claude-v1')
    zero = case.channel('Azure', 'newapi-3-azure-gpt-v1')
    case.fact(zero, '0')
    eur = case.channel('Google', 'newapi-41-vertex-gemini-v1')
    case.fact(eur, '9', unit='EUR')
    db.commit()
    result = menu(login('user'))
    missing = result[(case.categories['AWS'].id, 'aws_claude')]
    assert missing['channel_count'] == 1 and missing['data_status'] == 'missing'
    assert 'USD' not in missing['verified_usage_by_unit']
    assert Decimal(result[(case.categories['Azure'].id, 'azure_gpt')]['verified_usage_by_unit']['USD']) == 0
    assert 'USD' not in result[(case.categories['Google'].id, 'vertex_gemini')]['verified_usage_by_unit']
