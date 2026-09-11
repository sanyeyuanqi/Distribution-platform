# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008

from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import audit, require_roles, user_json
from ..db import get_db
from ..default_discounts import ensure_user_default_discounts
from ..models import Site, User
from ..remote_usage_totals import manual_usage_evidence, remote_usage_total
from ..security import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    hash_password,
    validate_password,
)

router = APIRouter(prefix='/users', tags=['Users'])


class UserCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    nickname: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    role: Literal['admin', 'user'] | None = None


class UserPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nickname: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, max_length=PASSWORD_MAX_LENGTH)
    active: bool | None = None

    @field_validator('password')
    @classmethod
    def check_password(cls, value):
        return validate_password(value) if value else value


def manageable(db: Session, actor: User, user_id: str) -> User:
    row = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if not row or row.archived or row.role == 'superadmin' or actor.role == 'user':
        raise HTTPException(404, 'User not found')
    if actor.role == 'admin' and (row.role != 'user' or row.parent_id != actor.id):
        raise HTTPException(404, 'User not found')
    return row


@router.get('')
def users(user: User = Depends(require_roles('superadmin', 'admin')), db: Session = Depends(get_db),
          offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), include_archived: bool = False,
          view: Literal['full', 'management'] = 'full', search: str = Query('', max_length=200),
          user_id: str | None = Query(None, max_length=64)):
    conditions = [User.role != 'superadmin']
    if user.role == 'admin':
        conditions += [User.parent_id == user.id, User.role == 'user']
    if not include_archived:
        conditions.append(User.archived.is_(False))
    if user_id is not None:
        conditions.append(User.id == user_id)
    if search.strip():
        term = '%' + search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        conditions.append(or_(cast(User.display_id, String).ilike(term, escape='\\'),
                              User.username.ilike(term, escape='\\'), User.nickname.ilike(term, escape='\\')))
    total = db.scalar(select(func.count()).select_from(User).where(*conditions))
    # List views need public account fields, not password hashes or session data.
    rows = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        User.id, User.display_id, User.username, User.nickname, User.role, User.parent_id,
        User.active, User.archived, User.created_at).where(*conditions)
        .order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit))]
    if not rows:
        return {'items': [], 'total': total}
    items = []
    # Both accepted view names expose the current management contract.
    from ..models_billing import SettlementOrder
    from ..models_channels import Channel, Distribution
    row_ids = {row.id for row in rows}
    teams = {row.id: {row.id} for row in rows}
    admin_ids = {row.id for row in rows if row.role == 'admin'}
    for child_id, parent_id in db.execute(select(User.id, User.parent_id).where(User.parent_id.in_(admin_ids))):
        teams[parent_id].add(child_id)
    owner_ids = set().union(*teams.values())
    parents = dict(db.execute(select(User.id, User.username).where(User.id.in_({r.parent_id for r in rows if r.parent_id}))).all())
    channel_counts = db.execute(select(Channel.owner_id, func.count().label('channel_count')).where(
        Channel.owner_id.in_(owner_ids), Channel.archived.is_(False)).group_by(Channel.owner_id)).all()
    local_counts = {}
    for count in channel_counts:
        local_counts[count.owner_id] = local_counts.get(count.owner_id, 0) + count.channel_count
    remote_counts = dict(db.execute(select(Channel.owner_id, func.count()).select_from(Distribution)
        .join(Channel, Distribution.channel_id == Channel.id).where(Channel.owner_id.in_(owner_ids),
        Distribution.remote_id.is_not(None)).group_by(Channel.owner_id)).all())
    # Current NewAPI observations are independent of historical settlement facts.
    # Batch only routing/usage metadata; do not load credentials or site tokens.
    distributions = [SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Distribution.id, Distribution.site_id, Distribution.remote_id, Distribution.status,
        Distribution.last_sync_at, Distribution.remote_snapshot, Channel.owner_id)
        .join(Channel, Distribution.channel_id == Channel.id).where(Channel.owner_id.in_(owner_ids)))]
    sites = {row.id: SimpleNamespace(**dict(row._mapping)) for row in db.execute(select(
        Site.id, Site.adapter, Site.base_url, Site.verified_at, Site.capabilities)
        .where(Site.id.in_({dist.site_id for dist in distributions})))} if distributions else {}
    evidence = manual_usage_evidence(db, distributions)
    distributions_by_owner = {}
    for dist in distributions:
        distributions_by_owner.setdefault(dist.owner_id, []).append(dist)
    order_conditions = [SettlementOrder.payee_id.in_(row_ids), SettlementOrder.status == 'settled']
    if user.role == 'admin':
        order_conditions.append(SettlementOrder.payer_id == user.id)
    # Count headers once regardless of line count, using the frozen recipient.
    order_totals = {row.payee_id: row for row in db.execute(select(SettlementOrder.payee_id,
        func.count().label('order_count'), func.sum(SettlementOrder.payment_amount).label('amount'))
        .where(*order_conditions).group_by(SettlementOrder.payee_id))}

    for row in rows:
        result = user_json(row)
        order_total = order_totals.get(row.id)
        order_amount = format(order_total.amount, 'f') if order_total else '0'
        if '.' in order_amount:
            order_amount = order_amount.rstrip('0').rstrip('.')
        owners = teams[row.id]
        current_usage = remote_usage_total(
            [dist for owner_id in owners for dist in distributions_by_owner.get(owner_id, [])],
            sites, evidence=evidence)
        if current_usage['total'] == 0:
            # No current remote targets is an empty sum; unread targets stay unknown.
            current_usage['amount'] = '0'
        result.update({
            'parent_username': parents.get(row.parent_id),
            'local_channels': sum(local_counts.get(owner_id, 0) for owner_id in owners),
            'remote_channels': sum(remote_counts.get(owner_id, 0) for owner_id in owners),
            'remote_usage_total': current_usage,
            'settlement_order_count': order_total.order_count if order_total else 0,
            'settlement_order_amount': order_amount,
            'settlement_order_unit': 'USDT',
        })
        items.append(result)
    return {'items': items, 'total': total}


@router.post('', status_code=201)
def create_user(body: UserCreate, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    role = body.role or 'admin'
    creator = lock_creation_actor(db, user)
    row = User(username=body.username, nickname=body.nickname, password_hash=hash_password(body.password),
               role=role, parent_id=creator.id)
    db.add(row)
    try:
        db.flush()
        ensure_user_default_discounts(db, row.id)
        audit(db, user, 'user.create', 'user', row.id, {'role': row.role, 'parent_id': row.parent_id})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Username already exists') from exc
    return user_json(row)


def lock_creation_actor(db: Session, actor: User) -> User:
    # Capture the authenticated session version before refreshing this same
    # identity-map row. Every new account belongs to its creating superadmin.
    actor_id, actor_version = actor.id, actor.session_version
    creator = db.scalar(select(User).where(User.id == actor_id).with_for_update()
                        .execution_options(populate_existing=True))
    if (not creator or not creator.active or creator.archived
            or creator.role != 'superadmin' or creator.session_version != actor_version):
        raise HTTPException(401, 'Session expired')
    return creator


@router.patch('/{user_id}')
def update_user(user_id: str, body: UserPatch, user: User = Depends(require_roles('superadmin', 'admin')), db: Session = Depends(get_db)):
    row = manageable(db, user, user_id)
    if body.nickname is not None:
        row.nickname = body.nickname
    if body.password:
        row.password_hash = hash_password(body.password)
        row.session_version += 1
    if body.active is not None and row.active != body.active:
        row.active = body.active
        row.session_version += 1
    audit(db, user, 'user.update', 'user', row.id, {'fields': sorted(body.model_fields_set), 'active': row.active})
    db.commit()
    return user_json(row)


@router.delete('/{user_id}')
def archive_user(user_id: str, user: User = Depends(require_roles('superadmin', 'admin')), db: Session = Depends(get_db)):
    row = manageable(db, user, user_id)
    if row.role == 'admin' and db.scalar(select(User.id).where(User.parent_id == row.id, User.archived.is_(False)).limit(1)):
        raise HTTPException(409, 'Archive all child accounts before archiving their administrator')
    row.active, row.archived = False, True
    row.session_version += 1
    audit(db, user, 'user.archive', 'user', row.id)
    db.commit()
    return user_json(row)
