"""Shared usage, account and order fixtures for retained features."""
from datetime import timedelta
from decimal import Decimal

import pytest
from app.db import uid, utcnow
from app.models import Category, CredentialFormat, Site
from app.models_billing import (
    DiscountVersion,
    SettlementOrder,
    SettlementOrderLine,
    UsageFact,
)
from app.models_channels import Channel, Distribution, UploadGroup
from app.security import encrypt, fingerprint


@pytest.fixture
def ledger(db, users):
    now = utcnow()
    cats = {}
    for name in ('OpenAI', 'Anthropic'):
        row = Category(name=name, family=name)
        db.add(row)
        db.flush()
        cats[name] = row
    site = Site(name='Fixture seller', prefix='FX', base_url='https://fixture.invalid',
        seller_user_id='1', token_encrypted=encrypt('fixture-seller-token'), enabled=True)
    db.add(site)
    db.flush()
    for category in cats.values():
        db.add(CredentialFormat(id=category.id, category_id=category.id, code='api_key-v1', name='Fixture format', version='1', enabled=True))
    db.flush()
    facts = []
    for who, cat, amount in [('admin', 'OpenAI', '150'), ('admin', 'Anthropic', '50'),
                              ('user', 'OpenAI', '600'), ('user', 'Anthropic', '400')]:
        group = UploadGroup(owner_id=users[who].id, category_id=cats[cat].id, format_id=cats[cat].id, tag=uid())
        db.add(group)
        db.flush()
        key = uid()
        channel = Channel(owner_id=users[who].id, group_id=group.id, category_id=cats[cat].id,
            format_id=cats[cat].id, key_encrypted=encrypt(key), key_hint='masked', fingerprint=fingerprint(key),
            created_at=now-timedelta(days=40))
        db.add(channel)
        db.flush()
        dist = Distribution(channel_id=channel.id, site_id=site.id, remote_id=uid(), remote_name='fixture', status='disabled')
        dist.remote_snapshot = {'id': dist.remote_id, 'status': 3}
        db.add(dist)
        db.flush()
        fact = UsageFact(source_id=uid(), site_id=site.id, distribution_id=dist.id, channel_id=channel.id,
            owner_id=users[who].id, admin_id=users['admin'].id, category_id=cats[cat].id,
            occurred_at=now-timedelta(days=10), raw_amount=Decimal(amount), raw_unit='USD', amount=Decimal(amount),
            unit='USD', conversion_version='contract-v1', verified=True, evidence='Fixture only')
        db.add(fact)
        facts.append(fact)
    for who, cat, percent in [('admin', 'OpenAI', '80'), ('admin', 'Anthropic', '90'),
                                ('user', 'OpenAI', '70'), ('user', 'Anthropic', '60')]:
        db.add(DiscountVersion(payer_id=users['root' if who == 'admin' else 'admin'].id,
            payee_id=users[who].id, category_id=cats[cat].id, layer='upper' if who == 'admin' else 'lower',
            percent=Decimal(percent), effective_at=now-timedelta(days=30)))
    db.commit()
    return {'cats': cats, 'facts': facts, 'site': site, 'now': now}

def _fact(ledger, who='user', category='OpenAI', users=None):
    return next(row for row in ledger['facts']
                if row.owner_id == users[who].id and row.category_id == ledger['cats'][category].id)

def _add_fact(db, template, amount='17', *, verified=True, period_end=None):
    row = UsageFact(source_id=uid(), site_id=template.site_id, distribution_id=template.distribution_id,
        channel_id=template.channel_id, owner_id=template.owner_id, admin_id=template.admin_id,
        category_id=template.category_id, occurred_at=template.occurred_at, period_end=period_end,
        raw_amount=Decimal(amount) if amount is not None else Decimal(17), raw_unit='USD',
        amount=Decimal(amount) if amount is not None else None, unit='USD',
        conversion_version='contract-v1', verified=verified)
    db.add(row)
    db.flush()
    return row

def _add_group(db, ledger, owner, *, admin_id, amount='31'):
    category_id = ledger['cats']['OpenAI'].id
    group = UploadGroup(owner_id=owner.id, category_id=category_id, format_id=category_id, tag=uid())
    db.add(group)
    db.flush()
    key = uid()
    channel = Channel(owner_id=owner.id, group_id=group.id, category_id=category_id,
        format_id=category_id, key_encrypted=encrypt(key), key_hint='test-only', fingerprint=fingerprint(key))
    db.add(channel)
    db.flush()
    distribution = Distribution(channel_id=channel.id, site_id=ledger['site'].id,
        remote_id=uid(), remote_name='selected-group-fixture', status='disabled')
    distribution.remote_snapshot = {'id': distribution.remote_id, 'status': 3}
    db.add(distribution)
    db.flush()
    fact = UsageFact(source_id=uid(), site_id=distribution.site_id, distribution_id=distribution.id,
        channel_id=channel.id, owner_id=owner.id, admin_id=admin_id, category_id=category_id,
        occurred_at=ledger['now']-timedelta(days=10), raw_amount=Decimal(amount), raw_unit='USD',
        amount=Decimal(amount), unit='USD', conversion_version='contract-v1', verified=True)
    db.add(fact)
    db.flush()
    return channel, fact


def order_for(db, users, channel, *, sources=None):
    """Persist a frozen order to test history preservation and deletion guards."""
    payer, payee = users['admin'], users['user']
    order = SettlementOrder(number=uid(), actor_id=payer.id, actor_name=payer.nickname,
        payer_id=payer.id, payer_name=payer.nickname, payee_id=payee.id, payee_name=payee.nickname,
        identity_snapshot={}, layer='lower', usage_amount=8, payment_amount=8,
        line_count=1, idempotency_key=uid(), request_hash=uid(), snapshot_hash=uid())
    db.add(order)
    db.flush()
    group = db.get(UploadGroup, channel.group_id)
    line = SettlementOrderLine(order_id=order.id, channel_id=channel.id, display_id=channel.display_id,
        group_id=channel.group_id, group_tag=group.tag, owner_id=channel.owner_id,
        owner_name=payee.nickname, owner_username=payee.username, category_id=channel.category_id,
        category_name='Fixture', variant='', service_name='Fixture', service_name_en='Fixture',
        discount_percent=100, usage_amount=8, payment_amount=8, site_amounts=sources or [])
    db.add(line)
    db.flush()
    return order, line
