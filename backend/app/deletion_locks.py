"""Serialize hard deletion with immutable financial snapshot construction."""
from fastapi import HTTPException
from sqlalchemy import func, select

_BILLING_PURGE_LOCK = 713947215163


def lock_billing_snapshot(db):
    # Shared readers preserve concurrent bill calculations, including historical
    # owners and sources inserted while the calculation is still running.
    db.execute(select(func.pg_advisory_xact_lock_shared(_BILLING_PURGE_LOCK)))


def lock_local_purge(db):
    if not db.scalar(select(func.pg_try_advisory_xact_lock(_BILLING_PURGE_LOCK))):
        raise HTTPException(409, '账单或关联记录正在处理，请稍后重新删除；本次未删除任何数据')
