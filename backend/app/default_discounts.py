"""Append-only 0% category baselines effective from each payee's creation."""
from fastapi import HTTPException
from sqlalchemy import select

from .billing import ZERO, naive, settlement_parties
from .models import Category, User
from .models_billing import DiscountVersion


def _ensure_user(db, user_id, category_ids):
    # Concurrent initializers for this payee must recheck after the prior writer
    # commits. Keep the lock and inserts in the caller's existing transaction.
    payee = db.scalar(select(User).where(User.id == user_id).with_for_update()
                      .execution_options(populate_existing=True))
    if payee is None or payee.role not in ('admin', 'user'):
        return 0
    try:
        payer, payee, layer = settlement_parties(db, payee, payee.id, write=False)
    except HTTPException as exc:
        if exc.status_code not in (404, 409):
            raise
        # Do not invent a payer for an orphaned historical account.
        return 0
    if ((layer == 'upper' and payer.role != 'superadmin')
            or (layer == 'lower' and (payer.role not in ('admin', 'superadmin') or payer.id == payee.id))):
        return 0
    effective_at = naive(payee.created_at)
    covered = set(db.scalars(select(DiscountVersion.category_id).where(
        DiscountVersion.payer_id == payer.id, DiscountVersion.payee_id == payee.id,
        DiscountVersion.layer == layer, DiscountVersion.service_variant.is_(None),
        DiscountVersion.category_id.in_(category_ids), DiscountVersion.effective_at <= effective_at)))
    missing = [category_id for category_id in category_ids if category_id not in covered]
    db.add_all([DiscountVersion(payer_id=payer.id, payee_id=payee.id, layer=layer,
        category_id=category_id, service_variant=None, inherits_category=False,
        percent=ZERO, effective_at=effective_at) for category_id in missing])
    if missing:
        db.flush()
    return len(missing)


def ensure_user_default_discounts(db, user_id):
    """Initialize one persisted user; append only, and never commit for the caller."""
    category_ids = list(db.scalars(select(Category.id).order_by(Category.id)))
    return _ensure_user(db, user_id, category_ids)


def ensure_all_default_discounts(db):
    """Idempotently fill legacy account gaps after the catalog/root exist."""
    category_ids = list(db.scalars(select(Category.id).order_by(Category.id)))
    user_ids = list(db.scalars(select(User.id).where(User.role.in_(('admin', 'user'))).order_by(User.id)))
    return sum(_ensure_user(db, user_id, category_ids) for user_id in user_ids)
