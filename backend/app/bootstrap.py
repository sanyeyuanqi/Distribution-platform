"""Idempotent catalog initialization and explicit initial superadmin provisioning."""
import getpass
import re
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .catalog_policy import (
    CATALOG_FAMILIES,
    get_catalog_format_specs,
)
from .config import settings
from .db import SessionLocal, utcnow
from .default_discounts import ensure_all_default_discounts
from .models import AuditEvent, Category, CredentialFormat, SiteUploadTemplate, User
from .models_channels import Channel
from .security import PASSWORD_MIN_LENGTH, hash_password

FAMILIES = CATALOG_FAMILIES


def _default_category(db: Session, family: str):
    """Locate the original category without overwriting administrator settings."""
    candidates = list(db.scalars(select(Category).where(Category.family == family)))
    category = next((row for row in candidates if row.name == family), None)
    if category is None:
        referenced = set(db.scalars(select(SiteUploadTemplate.category_id))) | set(db.scalars(select(Channel.category_id)))
        preferred = [row for row in candidates if row.id in referenced]
        if len(preferred) == 1:
            category = preferred[0]
        elif len(candidates) == 1:
            category = candidates[0]
        elif candidates:
            raise RuntimeError('同一接入类型有多个改名记录，无法唯一确定默认分类，请先核对历史关联')
        else:
            if db.scalar(select(Category.id).where(Category.name == family)):
                raise RuntimeError('默认分类名称已被其他类型占用，不能自动改写历史协议归属')
            category = Category(name=family, family=family, active=True)
            db.add(category)
            db.flush()
    return category


def seed_catalog(db: Session):
    """Install the fixed catalog without replacing historical credential identities."""
    for family in FAMILIES:
        _default_category(db, family).active = True
    seed_newapi_formats(db)
    seed_fixed_templates(db)


def seed_newapi_formats(db: Session):
    """Ensure every built-in definition exists while preserving historical parsers."""
    from .newapi_formats import protocol_schema

    categories = {family: _default_category(db, family) for family in FAMILIES}
    for spec in get_catalog_format_specs():
        category = categories[spec['family']]
        row = db.scalar(select(CredentialFormat).where(CredentialFormat.category_id == category.id,
            CredentialFormat.code == spec['code'], CredentialFormat.version == spec['version']))
        if row is None:
            row = CredentialFormat(category_id=category.id, code=spec['code'], name=spec['name'],
                version=spec['version'], schema_config=spec['schema_config'], enabled=True, default_models=[])
            db.add(row)
        else:
            try:
                same_protocol = protocol_schema(row.schema_config) == protocol_schema(spec['schema_config'])
            except ValueError:
                same_protocol = False
            if not same_protocol:
                # A formerly editable definition might be referenced by encrypted
                # credentials. Never reinterpret those credentials during startup.
                raise RuntimeError('内置凭据标识与历史解析规则冲突，不能自动改写已有密钥的解释方式')
            row.name = spec['name']
            row.schema_config = dict(spec['schema_config'])
            row.default_models = []
            row.enabled = True
        db.flush()


def seed_fixed_templates(db: Session):
    """Retire editable protocol defaults; retain receiving scopes and frozen tasks."""
    from .upload_templates import template_variant

    rows = list(db.execute(select(SiteUploadTemplate, Category, CredentialFormat)
        .join(Category, Category.id == SiteUploadTemplate.category_id)
        .join(CredentialFormat, CredentialFormat.id == SiteUploadTemplate.format_id)
        .where(Category.family.in_(FAMILIES))))
    destinations = set()
    changes = []
    for row, category, fmt in rows:
        variant = template_variant(category, fmt, row.models)
        destination = (row.site_id, row.category_id, variant)
        if destination in destinations:
            raise RuntimeError('同一站点已有重复的分类接收模板，不能自动合并或删除历史配置')
        destinations.add(destination)
        stored = row.channel_config or {}
        status = stored.get('status', 2)
        if type(status) is not int or status not in (1, 2):
            raise RuntimeError('模板渠道状态无效，不能自动调整其启用状态')
        fixed = {'status': status}
        fields = []
        if stored != fixed:
            fields.append('channel_config')
        if row.proxy_encrypted:
            fields.append('proxy')
        if row.variant != variant:
            fields.append('variant')
        if fields:
            changes.append((row, fixed, variant, fields))
    for row, config, variant, fields in changes:
        row.channel_config = config
        row.proxy_encrypted = None
        row.variant = variant
        row.version += 1
        row.updated_at = utcnow()
        db.add(AuditEvent(actor_role='system', action='upload_template.fixed_defaults',
            object_type='upload_template', object_id=row.id, site_id=row.site_id,
            summary={'fields': fields, 'version': row.version}))


def initialize(db: Session, username: str | None = None, password: str | None = None):
    if db.bind.dialect.name == 'postgresql':
        # Serialize bootstrap across API workers; no second initial superadmin race.
        db.execute(text('SELECT pg_advisory_xact_lock(713908213)'))
    seed_catalog(db)
    existing = db.scalar(select(User).where(User.role == 'superadmin'))
    if existing:
        ensure_all_default_discounts(db)
        db.commit()
        return existing
    username = username or settings.bootstrap_username
    password = password or settings.bootstrap_password
    if not username or not password:
        db.commit()
        raise RuntimeError('No superadmin exists. Set BOOTSTRAP_USERNAME and BOOTSTRAP_PASSWORD or run python -m app.bootstrap interactively')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', username):
        raise ValueError('Bootstrap username must be 3–64 letters, digits, dots, hyphens or underscores')
    row = User(username=username, nickname='超级管理员', password_hash=hash_password(password), role='superadmin', parent_id=None)
    db.add(row)
    db.flush()
    ensure_all_default_discounts(db)
    db.commit()
    return row


def bootstrap():
    with SessionLocal() as db:
        return initialize(db)


if __name__ == '__main__':
    # Schema is supplied by migrations. Never create/drop production tables here.
    if settings.bootstrap_username and settings.bootstrap_password:
        bootstrap()
    elif sys.stdin.isatty():
        name = input('初始超级管理员用户名：').strip()
        password = getpass.getpass(f'密码（至少 {PASSWORD_MIN_LENGTH} 位）：')
        if password != getpass.getpass('确认密码：'):
            raise SystemExit('两次输入的密码不一致')
        with SessionLocal() as db:
            initialize(db, name, password)
    else:
        raise SystemExit('Use a terminal or set BOOTSTRAP_USERNAME and BOOTSTRAP_PASSWORD')
