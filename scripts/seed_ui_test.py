"""Create an isolated UI acceptance database, never modify production data."""
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
config = dotenv_values(root / '.env')
os.environ['DATABASE_URL'] = make_url(config['DATABASE_URL']).set(database='keyacross_ui_test').render_as_string(hide_password=False)
sys.path.insert(0, str(root / 'backend'))
from app.db import Base, SessionLocal, uid, utcnow, engine
from app import models, models_channels, models_billing
from app.bootstrap import initialize
from app.models import User, Site, Category, CredentialFormat
from app.models_channels import Channel, UploadGroup, Distribution, KeyVersion
from app.models_billing import UsageFact, DiscountVersion
from app.security import encrypt, fingerprint, hash_password
from sqlalchemy import select

Base.metadata.create_all(engine)
with SessionLocal() as db:
    owner = initialize(db)
    if db.scalar(select(User).where(User.username == 'qa_admin')):
        print('Isolated UI test fixture already exists.')
        raise SystemExit()
    admin = User(username='qa_admin', nickname='验收管理员', password_hash=hash_password(config['BOOTSTRAP_PASSWORD']), role='admin', parent_id=owner.id)
    db.add(admin)
    db.flush()
    member = User(username='qa_member', nickname='验收子账号', password_hash=hash_password(config['BOOTSTRAP_PASSWORD']), role='user', parent_id=admin.id)
    db.add(member)
    db.flush()
    category = db.scalar(select(Category).where(Category.name == 'OpenAI'))
    fmt = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id))
    fmt.default_models = ['test-model']
    group = UploadGroup(owner_id=member.id, category_id=category.id, format_id=fmt.id, tag='B-QA-ISOLATED', name='验收专用分组')
    db.add(group)
    db.flush()
    channel = Channel(owner_id=member.id, group_id=group.id, category_id=category.id, format_id=fmt.id,
        key_encrypted=encrypt('fixture-only-not-a-real-secret'), key_hint='fixture••••', fingerprint=fingerprint('fixture-only-not-a-real-secret'),
        models=['test-model'], remark='仅用于隔离验收数据库，非真实业务数据', created_at=utcnow()-timedelta(days=30))
    db.add(channel)
    db.flush()
    db.add(KeyVersion(channel_id=channel.id, version=1, key_encrypted=channel.key_encrypted, fingerprint=channel.fingerprint))
    for i, amount in enumerate(['10', '20', '30']):
        site = Site(name=f'验收站点 {i+1}', prefix=f'QA{i}', base_url=f'https://qa-{i}.invalid', seller_user_id='1',
            token_encrypted=encrypt('fixture-only-seller'), enabled=False, health='unverified')
        db.add(site)
        db.flush()
        dist = Distribution(channel_id=channel.id, site_id=site.id, remote_id=str(i+1), remote_name=f'QA-fixture-{i}',
            status='disabled', models=['test-model'], usage_status='verified', created_at=utcnow()-timedelta(days=30))
        db.add(dist)
        db.flush()
        db.add(UsageFact(site_id=site.id, distribution_id=dist.id, source_id=uid(), channel_id=channel.id, owner_id=member.id,
            admin_id=admin.id, category_id=category.id, occurred_at=utcnow()-timedelta(days=1), raw_amount=Decimal(amount), raw_unit='USD',
            amount=Decimal(amount), unit='USD', conversion_version='qa-only', verified=True, evidence='Isolated acceptance fixture'))
    for payer, payee, layer, percent in [(owner, admin, 'upper', 80), (admin, member, 'lower', 70)]:
        db.add(DiscountVersion(payer_id=payer.id, payee_id=payee.id, category_id=category.id,
            layer=layer, percent=percent, effective_at=utcnow()-timedelta(days=10)))
    db.commit()
print('Isolated UI test data created in keyacross_ui_test. Production data is unchanged.')
