"""Independent adversarial review of monetary history and private source data."""
import csv
import io
from datetime import timedelta
from decimal import Decimal

from app.db import uid, utcnow
from app.models import Site
from app.models_channels import Distribution
from settlement_fixtures import ledger as _ledger_fixture

ledger = _ledger_fixture








def test_private_import_evidence_is_not_exposed_to_members(db, users, ledger, login):
    child_fact = next(f for f in ledger['facts'] if f.owner_id == users['user'].id)
    child_fact.evidence = 'Private seller contract token: sensitive-operator-evidence-1234'
    db.commit()
    response = login('user').get('/api/usage')
    assert response.status_code == 200
    assert 'sensitive-operator-evidence-1234' not in response.text
    assert all('evidence' not in row for row in response.json()['items'])


def test_usage_import_cannot_predate_distribution_adoption(db, users, ledger, login):
    distribution = db.get(Distribution, ledger['facts'][0].distribution_id)
    distribution.created_at = utcnow() - timedelta(days=20)
    distribution.adopted_at = utcnow() - timedelta(days=5)
    db.commit()
    response = login('root').post('/api/usage/import', json={'items': [{
        'distribution_id': distribution.id, 'source_id': uid(),
        'occurred_at': (utcnow() - timedelta(days=8)).isoformat() + 'Z',
        'raw_amount': '30', 'raw_unit': 'USD', 'amount': '30', 'unit': 'USD',
        'conversion_version': 'fixture-v1', 'verified': True,
        'evidence': 'Fixture remote interval before adoption'}]})
    assert response.status_code == 422, response.text


def test_import_cannot_claim_observed_interval_ending_in_future(db, users, ledger, login):
    distribution = db.get(Distribution, ledger['facts'][0].distribution_id)
    distribution.created_at = utcnow() - timedelta(days=20)
    db.commit()
    response = login('root').post('/api/usage/import', json={'items': [{
        'distribution_id': distribution.id, 'source_id': uid(),
        'occurred_at': (utcnow() - timedelta(days=1)).isoformat() + 'Z',
        'period_end': (utcnow() + timedelta(days=1)).isoformat() + 'Z',
        'raw_amount': '30', 'raw_unit': 'USD', 'amount': '30', 'unit': 'USD',
        'conversion_version': 'fixture-v1', 'verified': True, 'source_kind': 'exact_interval',
        'evidence': 'Fixture interval has not yet finished'}]})
    assert response.status_code == 422, response.text






def test_usage_export_enforces_same_scope_as_read(login, users, ledger):
    actor = login('admin')
    denied = actor.get('/api/usage/export', params={'owner_id': users['other_user'].id})
    assert denied.status_code == 404
    own = login('user').get('/api/usage/export')
    assert own.status_code == 200
    assert users['admin'].id not in own.text
    assert users['user'].id in own.text
    assert 'key_encrypted' not in own.text and 'evidence' not in own.text
    assert own.content.startswith(b'\xef\xbb\xbf')
    exported = list(csv.reader(io.StringIO(own.content.decode('utf-8-sig'))))
    assert exported[0] == ['消耗记录编号', '站点编号', '渠道编号', '归属用户编号', '分类编号', '消耗发生时间（UTC）',
        '原始消耗', '原始单位', '计价基数', '计价单位', '核实状态', '换算规则版本',
        '来源类型', '统计时区', '导出时间（UTC）']
    assert len(exported) == 3
    assert all(len(row) == 15 and row[3] == users['user'].id for row in exported[1:])
    assert {Decimal(row[8]) for row in exported[1:]} == {Decimal(600), Decimal(400)}
    assert all(row[10] == '已核实' and row[12] == '手动导入' for row in exported[1:])


def test_site_seller_identity_with_history_cannot_be_changed(login, ledger, db):
    actor = login('root')
    site = ledger['site']
    denied = actor.patch(f'/api/sites/{site.id}', json={'seller_user_id': '2'})
    assert denied.status_code == 409
    db.expire_all()
    assert db.get(Site, site.id).seller_user_id == '1'
    assert actor.patch(f'/api/sites/{site.id}', json={'seller_user_id': '1', 'enabled': False}).status_code == 200
