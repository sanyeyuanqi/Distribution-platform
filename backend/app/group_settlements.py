"""Authorized channel scopes and current settlement parties."""
from fastapi import HTTPException
from sqlalchemy import select

from .auth import scope_owner_ids
from .billing import settlement_parties
from .models_channels import Channel


def _channel_rows(db, actor, channel_ids):
    if not channel_ids or len(channel_ids) > 100:
        raise HTTPException(422, '请选择 1–100 个渠道标签分组')
    ids = list(dict.fromkeys(channel_ids))
    owners = scope_owner_ids(db, actor)
    rows = {row.id: row for row in db.execute(select(Channel.id, Channel.owner_id, Channel.category_id)
        .where(Channel.id.in_(ids), Channel.owner_id.in_(owners)))}
    if len(rows) != len(ids):
        raise HTTPException(404, '渠道标签分组不存在或不在授权范围内')
    return ids, rows


def _parties(db, actor, owner, users):
    if owner.role == 'superadmin':
        return None
    if actor.role == 'superadmin':
        parent = users.get(owner.parent_id)
        if owner.role == 'admin':
            payee = owner
        elif owner.role == 'user' and parent and parent.role == 'superadmin':
            # Direct ordinary users retain their existing lower-layer settlement.
            payee = owner
        elif owner.role == 'user' and parent and parent.role == 'admin':
            payee = parent
        else:
            return None
    else:
        payee = owner
    try:
        payer, payee, layer = settlement_parties(db, actor, payee.id, write=False)
    except HTTPException as exc:
        if exc.status_code not in (404, 409):
            raise
        return None
    if ((layer == 'upper' and (payer.role != 'superadmin' or payee.role != 'admin'))
            or (layer == 'lower' and (payer.role not in ('admin', 'superadmin') or payee.role != 'user'
                                      or payee.parent_id != payer.id))):
        return None
    return payer, payee, layer
