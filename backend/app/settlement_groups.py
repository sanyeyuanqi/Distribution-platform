"""Safe account-scoped channel group pages for the settlement center."""
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import String, cast, func, select

from .auth import scope_owner_ids
from .billing import serial
from .channel_services import service_details
from .models import Category, CredentialFormat, Site, User
from .models_channels import Channel, Distribution, UploadGroup
from .remote_usage_totals import manual_usage_evidence, remote_usage_total
from .settlement_orders import order_group_status


def account_groups(db, actor, account_id, search='', offset=0, limit=50):
    if actor.role not in ('superadmin', 'admin'):
        raise HTTPException(403, '当前账号无权访问结算中心')
    allowed = scope_owner_ids(db, actor)
    account = db.execute(select(User.id, User.username, User.nickname, User.role, User.parent_id)
        .where(User.id == account_id, User.id.in_(allowed))).first()
    if (account is None or account.role not in ('admin', 'user')
            or (actor.role == 'admin' and (account.role != 'user' or account.parent_id != actor.id))):
        raise HTTPException(404, '账号不存在或不在授权结算范围内')
    owners = [account.id]
    if account.role == 'admin':
        owners += list(db.scalars(select(User.id).where(User.parent_id == account.id,
                                                       User.role == 'user', User.id.in_(allowed))))
    query = select(Channel.id, Channel.display_id, Channel.owner_id, Channel.group_id, Channel.category_id,
                   Channel.created_at, Channel.archived, UploadGroup.tag.label('group_tag'),
                   Category.name.label('category_name'), User.nickname.label('owner_name'),
                   User.username.label('owner_username')).join(UploadGroup, UploadGroup.id == Channel.group_id)
    query = query.join(Category, Category.id == Channel.category_id).join(User, User.id == Channel.owner_id)
    query = query.where(Channel.owner_id.in_(owners))
    search = search.strip()
    if search:
        def pattern(value):
            return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'

        term = pattern(search)
        display_search = search[1:] if search.startswith('#') and search[1:].isdigit() else search
        query = query.where(UploadGroup.tag.ilike(term, escape='\\') | UploadGroup.name.ilike(term, escape='\\')
            | UploadGroup.remark.ilike(term, escape='\\') | Channel.remark.ilike(term, escape='\\')
            | User.username.ilike(term, escape='\\') | User.nickname.ilike(term, escape='\\')
            | cast(Channel.display_id, String).ilike(pattern(display_search), escape='\\'))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(db.execute(query.order_by(Channel.created_at.desc(), Channel.id.desc())
                          .offset(offset).limit(limit)).mappings())
    # Classify only the authorized page, including model-aware legacy services.
    # Classification inputs stay internal; expose the same three labels as channel management.
    services = {row.id: service_details(
        SimpleNamespace(family=row.family, name=row.category_name),
        SimpleNamespace(schema_config=row.schema_config), row.models)
        for row in db.execute(select(Channel.id, Channel.models, Category.family,
            Category.name.label('category_name'), CredentialFormat.schema_config)
            .join(Category, Category.id == Channel.category_id)
            .join(CredentialFormat, CredentialFormat.id == Channel.format_id)
            .where(Channel.id.in_([row['id'] for row in rows])))} if rows else {}
    statuses = {row['channel_id']: row for row in order_group_status(
        db, actor, [row['id'] for row in rows])} if rows else {}
    # Use the same remote observation evidence as channel management, independently
    # of verified settlement facts. Fetch only metadata for this authorized page.
    distributions = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Distribution.id, Distribution.channel_id, Distribution.site_id, Distribution.remote_id,
        Distribution.status, Distribution.last_sync_at, Distribution.remote_snapshot)
        .where(Distribution.channel_id.in_([row['id'] for row in rows])))] if rows else []
    sites = {row.id: SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Site.id, Site.adapter, Site.base_url, Site.verified_at, Site.capabilities)
        .where(Site.id.in_({dist.site_id for dist in distributions})))} if distributions else {}
    evidence = manual_usage_evidence(db, distributions)
    by_channel = {}
    for dist in distributions:
        by_channel.setdefault(dist.channel_id, []).append(dist)
    items = [{**dict(row), **services[row['id']], 'owner_name': row['owner_name'] or row['owner_username'],
              'settlement': statuses[row['id']],
              'remote_usage_total': remote_usage_total(by_channel.get(row['id'], []), sites, evidence=evidence)}
             for row in rows]
    return serial({'account': {key: getattr(account, key) for key in ('id', 'username', 'nickname', 'role')},
                   'items': items, 'total': total})
