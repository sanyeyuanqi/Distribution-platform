"""Register settlement orders; registration does not transfer money."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_roles
from ..db import get_db
from ..models import User
from ..settlement_orders import (
    create_order,
    get_order,
    list_orders,
    order_detail,
    preview_order,
)

router = APIRouter(prefix='/settlement-orders', tags=['settlement-orders'])
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]
Manager = Annotated[User, Depends(require_roles('superadmin', 'admin'))]
Identifier = Annotated[str, Field(min_length=1, max_length=36, strict=True)]


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    channel_ids: list[Identifier] = Field(min_length=1, max_length=100)

    @field_validator('channel_ids')
    @classmethod
    def unique_channels(cls, value):
        return sorted(set(value))


class CreateIn(PreviewIn):
    snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$', strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100, strict=True)


@router.post('/preview')
def preview(body: PreviewIn, db: DB, actor: Manager):
    return preview_order(db, actor, body.channel_ids)


@router.post('', status_code=201)
def create(body: CreateIn, db: DB, actor: Manager):
    order = create_order(db, actor, body.channel_ids, body.snapshot_hash, body.idempotency_key)
    db.commit()
    return order_detail(db, order)


@router.get('')
def listing(db: DB, actor: Actor, account_id: str | None = Query(default=None, min_length=1, max_length=36),
            search: str = Query(default='', max_length=200), offset: int = Query(default=0, ge=0),
            limit: int = Query(default=50, ge=1, le=100),
            payee_id: str | None = Query(default=None, min_length=1, max_length=36)):
    return list_orders(db, actor, account_id, search, offset, limit, payee_id=payee_id)


@router.get('/{order_id}')
def detail(db: DB, actor: Actor, order_id: str = Path(min_length=1, max_length=36)):
    return get_order(db, actor, order_id)
