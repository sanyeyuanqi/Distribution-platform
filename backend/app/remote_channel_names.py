"""Allocate readable remote names once, before any remote creation is queued."""
import hashlib

from fastapi import HTTPException
from sqlalchemy import func, select

from .models_channels import Distribution, TaskItem, UnclaimedChannel, UploadGroup

MAX_REMOTE_NAME_LENGTH = 160


def new_remote_channel_name(db, site, channel):
    group = db.get(UploadGroup, channel.group_id)
    if not group or not group.tag:
        raise HTTPException(422, '上传分组缺少自动化标签，无法生成远端渠道名称')
    base = f'{site.name}-{group.tag}'
    if len(base) > MAX_REMOTE_NAME_LENGTH:
        raise HTTPException(422, '站点名称与自动化标签合计超过 160 个字符，请缩短站点名称')

    # Serialize allocation for this exact site/name until the Distribution is
    # committed. No credential-derived identifier is needed in the public name.
    lock_id = int.from_bytes(hashlib.sha256(f'remote-name:{site.id}:{base}'.encode()).digest()[:8],
                             'big', signed=True)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(lock_id))):
        raise HTTPException(409, '同一上传分组的远端名称正在分配，请稍后重试')

    used = set(db.scalars(select(Distribution.remote_name).where(
        Distribution.site_id == site.id, Distribution.remote_name.startswith(base, autoescape=True))))
    used.update(db.scalars(select(UnclaimedChannel.remote_name).where(
        UnclaimedChannel.site_id == site.id, UnclaimedChannel.remote_name.startswith(base, autoescape=True))))
    cleanup_name = TaskItem.snapshot['delete_target']['name'].as_string()
    used.update(db.scalars(select(cleanup_name).where(
        TaskItem.site_id == site.id, TaskItem.operation == 'remote_cleanup',
        TaskItem.snapshot['delete_acknowledged'].as_boolean().is_not(True),
        cleanup_name.startswith(base, autoescape=True))))

    name, ordinal = base, 1
    while name in used:
        ordinal += 1
        name = f'{base}-{ordinal}'
    if len(name) > MAX_REMOTE_NAME_LENGTH:
        raise HTTPException(422, '站点名称、自动化标签与区分序号合计超过 160 个字符，请缩短站点名称')
    return name
