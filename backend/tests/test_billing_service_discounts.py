"""Service-scoped discounts affect real imported usage and settlement amounts."""
from datetime import timedelta
from decimal import Decimal

import pytest
from app.catalog_policy import get_catalog_format_specs
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_billing import DiscountVersion, UsageFact
from app.models_channels import Channel, Distribution, UploadGroup
from app.routers.stats import ImportIn, import_facts
from app.security import encrypt, fingerprint
from sqlalchemy import select

SERVICE_FORMATS = [
    ('AWS', 'newapi-33-aws-bedrock-v1', 'bedrock'),
    ('AWS', 'newapi-14-aws-claude-v1', 'aws_claude'),
    ('Anthropic', 'newapi-14-api-key-v1', ''),
    ('OpenAI', 'api_key-v1', ''),
    ('Azure', 'newapi-3-azure-gpt-v1', 'azure_gpt'),
    ('Azure', 'newapi-14-azure-claude-v1', 'azure_claude'),
    ('Google', 'newapi-24-api-key-v1', 'ai_studio_gemini'),
    ('Google', 'newapi-41-vertex-gemini-v1', 'vertex_gemini'),
    ('Google', 'newapi-41-vertex-claude-v1', 'vertex_claude'),
    ('OpenRouter', 'newapi-20-api-key-v1', ''),
    ('OpenCode', 'newapi-14-api-key-v1', ''),
]


@pytest.fixture
def service_ledger(db, users):
    now = utcnow()
    categories, services = {}, {}
    site = Site(name='Service billing fixture', prefix='SERVICE', base_url='https://fixture.invalid',
                seller_user_id='1', token_encrypted=encrypt('fixture-only-system-token'))
    db.add(site)
    db.flush()
    specs = get_catalog_format_specs()
    for family, code, variant in SERVICE_FORMATS:
        if family not in categories:
            category = Category(name=family, family=family)
            db.add(category)
            db.flush()
            categories[family] = category
        category = categories[family]
        spec = next(spec for spec in specs if spec['family'] == family and spec['code'] == code)
        fmt = CredentialFormat(category_id=category.id, code=code, version='1', name=code,
                               enabled=True, schema_config=spec['schema_config'])
        db.add(fmt)
        db.flush()
        group = UploadGroup(owner_id=users['user'].id, category_id=category.id, format_id=fmt.id, tag=uid())
        db.add(group)
        db.flush()
        key = uid()
        channel = Channel(owner_id=users['user'].id, group_id=group.id, category_id=category.id,
                          format_id=fmt.id, key_encrypted=encrypt(key), key_hint='fixture',
                          fingerprint=fingerprint(key), created_at=now - timedelta(days=40))
        db.add(channel)
        db.flush()
        distribution = Distribution(channel_id=channel.id, site_id=site.id, remote_id=uid(),
                                    remote_name='fixture', created_at=now - timedelta(days=35))
        db.add(distribution)
        db.flush()
        services[(family, variant)] = {'format': fmt, 'channel': channel, 'distribution': distribution}
    db.commit()
    return {'now': now, 'categories': categories, 'services': services, 'site': site}


def add_rate(db, users, ledger, family, percent, variant=None, days=-30):
    rate = DiscountVersion(payer_id=users['admin'].id, payee_id=users['user'].id,
                           category_id=ledger['categories'][family].id, layer='lower',
                           service_variant=variant, percent=Decimal(percent),
                           effective_at=ledger['now'] + timedelta(days=days))
    db.add(rate)
    return rate


def import_services(db, users, ledger, service_keys=None):
    selected = service_keys or list(ledger['services'])
    body = ImportIn(items=[{
        'distribution_id': ledger['services'][key]['distribution'].id, 'source_id': uid(),
        'occurred_at': ledger['now'] - timedelta(days=10), 'raw_amount': '100', 'raw_unit': 'USD',
        'amount': '100', 'unit': 'USD', 'verified': True, 'conversion_version': 'fixture-v1',
        'evidence': 'Independent service usage fixture',
    } for key in selected])
    result = import_facts(body, db, users['root'])
    return [db.get(UsageFact, item['id']) for item in result['items']]




def test_import_freezes_all_eleven_services_and_later_format_changes_do_not_reprice_facts(db, users, service_ledger):
    for family in service_ledger['categories']:
        add_rate(db, users, service_ledger, family, '70')
    add_rate(db, users, service_ledger, 'AWS', '20', 'bedrock', -20)
    add_rate(db, users, service_ledger, 'AWS', '40', 'aws_claude', -20)
    facts = import_services(db, users, service_ledger)
    assert [fact.service_variant for fact in facts] == [row[2] for row in SERVICE_FORMATS]
    bedrock = service_ledger['services'][('AWS', 'bedrock')]
    bedrock['format'].schema_config = {'type': 'aws_claude', 'remote_type': 14}
    bedrock['channel'].models = ['a-different-model']
    db.commit()
    db.expire_all()
    assert [db.get(UsageFact, fact.id).service_variant for fact in facts] == [row[2] for row in SERVICE_FORMATS]
    assert all(db.get(UsageFact, fact.id).amount == Decimal(100) for fact in facts)




def test_discount_api_projects_eleven_services_and_append_only_inheritance(db, users, service_ledger, login):
    client = login('admin')
    category_id = service_ledger['categories']['AWS'].id
    base = client.post('/api/discounts', json={'payee_id': users['user'].id, 'category_id': category_id, 'percent': '80'})
    assert base.status_code == 201, base.text
    fine = client.post('/api/discounts', json={'payee_id': users['user'].id, 'category_id': category_id,
                                            'service_variant': 'bedrock', 'percent': '20'})
    assert fine.status_code == 201, fine.text
    payload = client.get('/api/discounts', params={'payee_id': users['user'].id}).json()
    assert len(payload['services']) == 11
    bedrock = next(row for row in payload['services'] if row['service_variant'] == 'bedrock')
    claude = next(row for row in payload['services'] if row['service_variant'] == 'aws_claude')
    assert bedrock['current']['percent'] == '20' and not bedrock['inherited']
    assert claude['current']['percent'] == '80' and claude['inherited']
    reset = client.post('/api/discounts', json={'payee_id': users['user'].id, 'category_id': category_id,
                                             'service_variant': 'bedrock', 'inherits_category': True})
    assert reset.status_code == 201, reset.text
    payload = client.get('/api/discounts', params={'payee_id': users['user'].id}).json()
    bedrock = next(row for row in payload['services'] if row['service_variant'] == 'bedrock')
    assert bedrock['inherited'] and bedrock['current']['id'] == base.json()['id']
    assert bedrock['override']['id'] == reset.json()['id']
    assert len(payload['items']) == 3
    assert db.get(DiscountVersion, fine.json()['id']).percent == Decimal(20)


@pytest.mark.parametrize('fields', [
    {}, {'service_variant': 'bedrock'}, {'service_variant': 'azure_gpt', 'percent': '20'},
    {'service_variant': 'bedrock', 'inherits_category': True, 'percent': '0'},
    {'inherits_category': True}, {'service_variant': 'bedrock', 'percent': '100.000001'},
    {'service_variant': 'bedrock', 'percent': '-1'}, {'service_variant': 'bedrock', 'percent': '20.0000001'},
])
def test_discount_api_rejects_invalid_scope_and_percentage_contracts(db, users, service_ledger, login, fields):
    response = login('admin').post('/api/discounts', json={
        'payee_id': users['user'].id, 'category_id': service_ledger['categories']['AWS'].id, **fields})
    assert response.status_code == 422, response.text
    assert not list(db.scalars(select(DiscountVersion)))


def test_service_discount_permissions_remain_with_the_actual_payer(db, users, service_ledger, login):
    payload = {'payee_id': users['user'].id, 'category_id': service_ledger['categories']['AWS'].id,
               'service_variant': 'bedrock', 'percent': '20'}
    assert login('user').post('/api/discounts', json=payload).status_code == 404
    assert login('other_admin').post('/api/discounts', json=payload).status_code == 404
