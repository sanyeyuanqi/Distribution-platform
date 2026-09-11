# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_roles
from ..db import get_db
from ..models import AuditEvent, User

router = APIRouter(prefix='/audit', tags=['Audit'])


@router.get('')
def audit_events(user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db),
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                 action: str | None = None, object_id: str | None = None):
    conditions = []
    if action:
        conditions.append(AuditEvent.action == action)
    if object_id:
        conditions.append(AuditEvent.object_id == object_id)
    total = db.scalar(select(func.count()).select_from(AuditEvent).where(*conditions))
    rows = db.scalars(select(AuditEvent).where(*conditions).order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit))
    return {'items': [{name: getattr(row, name) for name in ('id', 'actor_id', 'actor_role', 'action', 'object_type',
                      'object_id', 'site_id', 'result', 'summary', 'created_at')} for row in rows], 'total': total}
