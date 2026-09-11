"""Shared settlement identities, rate selection and serialization."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing_services import scoped_discount
from .models import User

ZERO = Decimal(0)



def serial(value):
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    if isinstance(value, datetime):
        return value.isoformat() + ('Z' if value.tzinfo is None else '')
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(v) for v in value]
    return value


def record(obj, omit=()):
    return serial({c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in omit})


def naive(value: datetime | None):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def digest(value):
    return hashlib.sha256(json.dumps(serial(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def settlement_parties(db: Session, actor: User, payee_id: str, write=True):
    payee = db.get(User, payee_id)
    if payee is None or payee.role == 'superadmin':
        raise HTTPException(404, '收款对象不存在')
    if payee.role == 'admin':
        payer = db.scalar(select(User).where(User.role == 'superadmin'))
        layer = 'upper'
    else:
        payer = db.get(User, payee.parent_id)
        layer = 'lower'
    if payer is None:
        raise HTTPException(409, '账户归属不完整')
    permitted = actor.id == payer.id if write else actor.id in (payer.id, payee.id) or actor.role == 'superadmin'
    if not permitted:
        raise HTTPException(404, '收款对象不在授权结算范围内')
    return payer, payee, layer


def applicable_discount(history, at, service_variant=None):
    return scoped_discount(history, at, service_variant)
