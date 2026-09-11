# FastAPI intentionally declares dependency callables in defaults.
# ruff: noqa: B008
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import audit, require_roles
from ..catalog_policy import (
    catalog_category_filter,
    catalog_category_order,
    catalog_format_spec,
    is_catalog_category,
)
from ..channel_service import (
    SubmitInput,
    UploadInput,
    channel_snapshot,
    new_task,
    notify_worker,
    prepare_upload,
    refresh_task,
    task_json,
)
from ..credential_containers import (
    bundle_hint,
    bundle_identity,
    channel_partitions,
    encode_entries,
    replace_member_index,
)
from ..db import get_db, uid
from ..models import Category, CredentialFormat, User
from ..models_channels import (
    Channel,
    Distribution,
    KeyVersion,
    Task,
    TaskItem,
    UploadGroup,
)
from ..remote_channel_names import new_remote_channel_name
from ..security import encrypt, fingerprint
from ..upload_templates import (
    SimpleSubmitInput,
    SimpleUploadInput,
    batch_fingerprint,
    batch_group_tag,
    distribution_template_snapshot,
    effective_template,
    find_batch_task,
    new_upload_group_tag,
    prepare_simple_upload,
    resolve_templates,
    template_config,
    upload_settings,
)

router = APIRouter(prefix='/uploads', tags=['uploads'])


@router.get('/options')
def simple_options(user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    from ..config import settings
    categories = list(db.scalars(select(Category).where(catalog_category_filter(), Category.active.is_(True))
                                .order_by(catalog_category_order())))
    items = [resolve_templates(db, category.id)[0] for category in categories]
    return {'items': items, 'total': len(items), 'upload_limit': settings.upload_limit}


@router.get('/label')
def simple_label(category_id: str, batch_token: str = Query(min_length=8, max_length=120),
                 format_id: str | None = None,
                 user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if not is_catalog_category(category) or not category.active:
        raise HTTPException(422, '分类不存在或已停用')
    fmt = db.get(CredentialFormat, format_id) if format_id else None
    if format_id and (not catalog_format_spec(category, fmt) or not fmt.enabled):
        raise HTTPException(422, '所选凭据格式不属于此分类的可用格式')
    if fmt is None:
        # Old callers did not send a format. Follow the same default resolver,
        # including before any receiving template becomes ready.
        fmt = resolve_templates(db, category.id)[3]
    return {'group_tag': batch_group_tag(db, user, category, batch_token, fmt)}


@router.post('/simple-preview')
def simple_preview(payload: SimpleUploadInput, user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    return prepare_simple_upload(db, user, payload)[0]


def add_template_distribution(db, task, channel, fmt, template, site):
    existing = set(db.scalars(select(Distribution.partition_key).where(
        Distribution.channel_id == channel.id, Distribution.site_id == site.id)))
    for part in channel_partitions(channel, fmt, site=site):
        if part['partition_key'] in existing:
            continue
        proxy = (encrypt(part['entries'][0]['proxy']) if part['entries'][0]['proxy'] else None
                 ) if channel.key_mode == 'multiple' else channel.proxy_encrypted
        history, snap = distribution_template_snapshot(channel, fmt, site, template,
            category=db.get(Category, channel.category_id), partition_key=part['partition_key'], partition_proxy_encrypted=proxy)
        name = new_remote_channel_name(db, site, channel)
        dist = Distribution(id=uid(), channel_id=channel.id, site_id=site.id,
                            remote_name=name, key_version=channel.key_version,
                            partition_key=part['partition_key'], partition_label=part['partition_label'], key_count=part['key_count'],
                            models=snap['models'], routing_group=template.routing_group,
                            upload_template_id=template.id, template_version=template.version,
                            template_snapshot=history)
        db.add(dist)
        db.flush()
        db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id, distribution_id=dist.id,
                        operation='create', key_version=channel.key_version, snapshot=snap, proxy_encrypted=proxy))


def simple_task_json(db, task):
    group = db.get(UploadGroup, task.group_id) if task.group_id else None
    return {**task_json(db, task, details=True),
            'group_tag': task.snapshot.get('group_tag') or (group.tag if group else None)}


def lock_catalog_for_upload(db, category_id):
    # Catalog edits take the same category-first lock before checking references.
    # Keep it until the first channel/group references have been committed.
    db.scalar(select(Category).where(Category.id == category_id).with_for_update()
              .execution_options(populate_existing=True))
    list(db.scalars(select(CredentialFormat).where(CredentialFormat.category_id == category_id)
                    .order_by(CredentialFormat.id).with_for_update().execution_options(populate_existing=True)))


@router.post('/simple-submit')
def simple_submit(payload: SimpleSubmitInput, user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    db.scalar(select(User).where(User.id == user.id).with_for_update(key_share=True))
    request_values = payload.model_dump(exclude={'idempotency_key', 'configuration_revision'})
    # New optional fields must not invalidate retries of older persisted requests.
    if not payload.inventory:
        request_values.pop('inventory')
    if payload.batch_token is None:
        request_values.pop('batch_token')
    if payload.api_base_url is None:
        request_values.pop('api_base_url')
    request_hash = fingerprint(json.dumps({'mode': 'template', **request_values},
                                         sort_keys=True, ensure_ascii=False))
    prior = db.scalar(select(Task).where(Task.actor_id == user.id, Task.idempotency_key == payload.idempotency_key))
    if prior:
        if prior.request_hash != request_hash:
            raise HTTPException(409, '该幂等标识已用于不同的上传内容')
        return simple_task_json(db, prior)
    prior_batch = find_batch_task(db, user, payload.category_id, payload.batch_token)
    if prior_batch:
        if prior_batch.request_hash != request_hash:
            raise HTTPException(409, '该批次已提交其他上传内容，请开始新批次')
        return simple_task_json(db, prior_batch)
    lock_catalog_for_upload(db, payload.category_id)
    result, rows, targets, category, fmt = prepare_simple_upload(db, user, payload)
    if payload.configuration_revision and payload.configuration_revision != result['configuration_revision']:
        raise HTTPException(409, '模板或目标站点已发生变化，请重新检查分发预览后提交')
    if not result['can_submit']:
        raise HTTPException(422, {'message': '上传校验未通过', 'preview': result})
    group = None
    if any(not row.get('existing_channel_id') for row in rows):
        group_id = uid()
        tag = result['group_tag'] or new_upload_group_tag(db, user, category, fmt)
        group = UploadGroup(id=group_id, owner_id=user.id, category_id=category.id, format_id=fmt.id, tag=tag, name=tag)
        db.add(group)
        db.flush()
    task = new_task(db, user, user.id, 'upload', group_id=group.id if group else None,
                    idempotency_key=payload.idempotency_key, request_hash=request_hash,
                    snapshot={'upload_mode': 'template', 'category_id': category.id, 'format_id': fmt.id,
                              'format_version': fmt.version, 'enable_strategy': result['enable_strategy'],
                              'group_tag': result['group_tag'] or (group.tag if group else None),
                              **({'batch_fingerprint': batch_fingerprint(user, category.id, payload.batch_token)}
                                 if payload.batch_token else {}),
                              'site_ids': [site.id for _, site in targets],
                              'configuration_revision': result['configuration_revision'],
                              'templates': [template_config(template) for template, _ in targets]})
    new_rows = [row for row in rows if not row.get('existing_channel_id')]
    submitted_rows = [row for row in rows if row.get('existing_channel_id')]
    if new_rows:
        entries = new_rows
        multiple = len(entries) > 1
        first = entries[0]
        channel = Channel(id=uid(), owner_id=user.id, group_id=group.id, category_id=category.id,
            format_id=fmt.id, key_encrypted=encrypt(encode_entries(entries) if multiple else first['key']),
            key_hint=bundle_hint(entries, fmt) if multiple else first['key_hint'],
            fingerprint=bundle_identity(entries, fmt) if multiple else first['fingerprint'],
            key_mode='multiple' if multiple else 'single', key_count=len(entries), key_version=1,
            models=list(dict.fromkeys(model for template, _ in targets
                for model in effective_template(template, upload_settings(payload))['models'])),
            remark=first['remark'] if all(row['remark'] == first['remark'] for row in entries) else '',
            upload_mode='template', upload_settings=upload_settings(payload),
            proxy_encrypted=encrypt(first['proxy']) if first.get('proxy') and all(row['proxy'] == first['proxy'] for row in entries) else None)
        db.add(channel)
        db.flush()
        replace_member_index(db, channel, entries, fmt)
        db.add(KeyVersion(channel_id=channel.id, version=1, key_encrypted=channel.key_encrypted, fingerprint=channel.fingerprint))
        submitted_rows.append({'existing_channel_id': channel.id, 'missing_site_ids': [site.id for _, site in targets]})
    processed = set()
    for row in submitted_rows:
        if row['existing_channel_id'] in processed:
            continue
        processed.add(row['existing_channel_id'])
        channel = db.get(Channel, row['existing_channel_id'])
        for template, site in targets:
            if site.id in row['missing_site_ids']:
                add_template_distribution(db, task, channel, db.get(CredentialFormat, channel.format_id), template, site)
    db.flush()
    refresh_task(db, task)
    audit(db, user, 'upload.simple_submit', 'task', task.id,
          {'new_group_id': task.group_id, 'rows': len(rows), 'targets': len(targets), 'category_id': category.id})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '上传内容与同时提交的任务冲突，请刷新后重试') from None
    notify_worker()
    return simple_task_json(db, task)


@router.post('/preview')
def preview(payload: UploadInput, user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    return prepare_upload(db, user, payload)[0]


@router.post('/submit')
def submit(payload: SubmitInput, user=Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    # Per-owner transaction lock serializes concurrent dedup and idempotency checks.
    db.scalar(select(User).where(User.id == user.id).with_for_update(key_share=True))
    request_hash = fingerprint(json.dumps(payload.model_dump(exclude={'idempotency_key'}), sort_keys=True, ensure_ascii=False))
    prior = db.scalar(select(Task).where(Task.actor_id == user.id, Task.idempotency_key == payload.idempotency_key))
    if prior:
        if prior.request_hash != request_hash:
            raise HTTPException(409, '该幂等标识已用于不同的上传内容')
        return task_json(db, prior, details=True)
    lock_catalog_for_upload(db, payload.category_id)
    result, rows, sites, category, fmt = prepare_upload(db, user, payload)
    if not (result['can_submit'] or payload.allow_partial and result['can_submit_partial']):
        raise HTTPException(422, {'message': '上传校验未通过', 'preview': result})
    group = None
    if any(not row.get('existing_channel_id') for row in rows):
        group_id = uid()
        tag = new_upload_group_tag(db, user, category, fmt)
        group = UploadGroup(id=group_id, owner_id=user.id, category_id=category.id, format_id=fmt.id, tag=tag, name=tag)
        db.add(group)
        db.flush()
    task = new_task(db, user, user.id, 'upload', group_id=group.id if group else None,
                    idempotency_key=payload.idempotency_key, request_hash=request_hash,
                    snapshot={'site_ids': [s.id for s in sites], 'category_id': category.id, 'format_id': fmt.id,
                              'format_version': fmt.version, 'models': result['models'], 'allow_partial': payload.allow_partial,
                              'enable_strategy': payload.enable_strategy})
    for row in rows:
        if row.get('existing_channel_id'):
            channel = db.get(Channel, row['existing_channel_id'])
        else:
            channel = Channel(id=uid(), owner_id=user.id, group_id=group.id, category_id=category.id,
                              format_id=fmt.id, key_encrypted=encrypt(row['key']), key_hint=row['key_hint'],
                              fingerprint=row['fingerprint'], key_version=1, models=result['models'],
                              remark=row['remark'], declaration=payload.declaration,
                              proxy_encrypted=encrypt(row['proxy']) if row['proxy'] else None)
            db.add(channel)
            db.flush()
            replace_member_index(db, channel, [row], fmt)
            db.add(KeyVersion(channel_id=channel.id, version=1, key_encrypted=channel.key_encrypted, fingerprint=channel.fingerprint))
        for site in sites:
            if site.id not in row['missing_site_ids']:
                continue
            name = new_remote_channel_name(db, site, channel)
            dist = Distribution(id=uid(), channel_id=channel.id, site_id=site.id,
                                remote_name=name, key_version=channel.key_version, models=result['models'])
            db.add(dist)
            db.flush()
            target = next(t for t in result['targets'] if t['id'] == site.id)
            snap = channel_snapshot(channel, fmt, site)
            snap.update(enable_strategy=payload.enable_strategy, rpm_enabled=payload.rpm_enabled,
                        rpm_limit=payload.rpm_limit, proxy_requested=bool(row['proxy']))
            db.add(TaskItem(task_id=task.id, channel_id=channel.id, site_id=site.id,
                            distribution_id=dist.id, operation='create', key_version=channel.key_version,
                            snapshot=snap, status='pending' if target['compatible'] else 'failed',
                            stage='queued' if target['compatible'] else 'validation',
                            error=None if target['compatible'] else '；'.join(target['issues'])))
    db.flush()
    refresh_task(db, task)
    audit(db, user, 'upload.submit', 'task', task.id, {'new_group_id': task.group_id, 'rows': len(rows), 'targets': len(sites)})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '上传内容与同时提交的任务冲突，请刷新后重试') from None
    notify_worker()
    return task_json(db, task, details=True)
