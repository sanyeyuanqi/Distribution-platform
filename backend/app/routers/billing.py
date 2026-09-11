"""Current settlement rates and account channel-group selection."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, get_current_user, require_roles
from ..billing import ZERO, naive, record, serial, settlement_parties
from ..billing_services import (
    service_definitions,
    service_projection,
    valid_service_variant,
)
from ..db import get_db, utcnow
from ..models import Category, User
from ..models_billing import DiscountVersion
from ..settlement_groups import account_groups

router = APIRouter(tags=['settlements'])
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]



class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class DiscountIn(Strict):
    payee_id: str
    category_id: str
    service_variant: str | None = Field(default=None, max_length=32, strict=True)
    inherits_category: bool = Field(default=False, strict=True)
    percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=9, decimal_places=6)
    effective_at: datetime | None = None

    @model_validator(mode='after')
    def valid_scope(self):
        if self.inherits_category:
            if self.service_variant is None or self.percent is not None:
                raise ValueError('恢复大类默认须指定细分类，且不能同时填写百分比')
        elif self.percent is None:
            raise ValueError('请填写 0–100 的折扣百分比')
        return self


@router.get('/discounts')
def discounts(payee_id: str, db: DB, actor: Actor):
    payer, payee, layer = settlement_parties(db, actor, payee_id, write=False)
    items = list(db.scalars(select(DiscountVersion).where(DiscountVersion.payer_id == payer.id,
        DiscountVersion.payee_id == payee.id).order_by(
            DiscountVersion.effective_at, DiscountVersion.created_at, DiscountVersion.id)))
    now = utcnow()
    definitions = service_definitions(db.scalars(select(Category)))
    services = [service_projection(definition, [version for version in items
                if version.category_id == definition['category_id']], now, record) for definition in definitions]
    return serial({'items': [record(x) for x in reversed(items)], 'total': len(items), 'layer': layer,
                   'as_of': now, 'services': services})


@router.post('/discounts', status_code=201)
def set_discount(body: DiscountIn, db: DB, actor: Actor):
    payer, payee, layer = settlement_parties(db, actor, body.payee_id)
    # Rate changes and order confirmations share the same settlement boundary.
    db.execute(select(User.id).where(User.id == payee.id).with_for_update(key_share=True)).all()
    category = db.get(Category, body.category_id)
    if not category:
        raise HTTPException(404, '分类不存在')
    if body.service_variant is not None and not valid_service_variant(category, body.service_variant):
        raise HTTPException(422, '细分类与所选渠道分类不匹配')
    effective = naive(body.effective_at) or utcnow()
    if body.effective_at and effective < utcnow():
        raise HTTPException(422, '不能追溯改写历史汇率；请设置当前或未来生效时间')
    row = DiscountVersion(payer_id=payer.id, payee_id=payee.id, category_id=body.category_id,
        service_variant=body.service_variant, inherits_category=body.inherits_category,
        layer=layer, percent=body.percent if body.percent is not None else ZERO, effective_at=effective)
    db.add(row)
    db.flush()
    audit(db, actor, 'discount.create', 'discount', row.id, {'payee_id': payee.id, 'category_id': body.category_id,
        'service_variant': body.service_variant, 'inherits_category': body.inherits_category,
        'percent': str(body.percent) if body.percent is not None else None})
    db.commit()
    return record(row)


@router.get('/settlements/groups', dependencies=[Depends(require_roles('superadmin', 'admin'))])
def settlement_groups(db: DB, actor: Actor, account_id: Annotated[str, Query(min_length=1, max_length=36)],
                      search: Annotated[str, Query(max_length=200)] = '',
                      offset: Annotated[int, Query(ge=0)] = 0,
                      limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return account_groups(db, actor, account_id, search, offset, limit)
