# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, not_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from ..adapters import get_adapter
from ..auth import audit, get_current_user, require_roles
from ..db import get_db, utcnow
from ..models import Site, User
from ..network import validate_remote_url
from ..security import encrypt, mask
from ..site_distribution import distribution_readiness, stop_unready_distribution
from ..site_verification import (
    connection_check_result,
    safe_diagnostic,
    verification_state,
)

router = APIRouter(prefix='/sites', tags=['Sites'])


def single_routing_group(value: str) -> str:
    if ',' in value or '\n' in value or '\r' in value:
        raise ValueError('路由组必须为单个组名，不能包含逗号或换行')
    value = value.strip()
    if not value or len(value) > 160:
        raise ValueError('路由组名称需要 1 至 160 个字符')
    return value


RoutingGroup = Annotated[str, AfterValidator(single_routing_group)]


class SiteCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    prefix: str = Field(min_length=1, max_length=32, pattern=r'^[A-Za-z0-9_-]+$')
    base_url: str = Field(min_length=8, max_length=1000)
    adapter: Literal['silicon-v1', 'tcp-red-v1', 'new-api-v1', 'spacex-hub-v1'] = 'silicon-v1'
    routing_group: RoutingGroup = 'default'
    seller_user_id: str = Field(min_length=1, max_length=20, pattern=r'^[1-9][0-9]*$')
    token: str = Field(min_length=1, max_length=10000)
    enabled: bool = False
    collect_enabled: bool = True
    stats_config: dict = Field(default_factory=dict)


class SitePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prefix: str | None = Field(default=None, min_length=1, max_length=32, pattern=r'^[A-Za-z0-9_-]+$')
    base_url: str | None = Field(default=None, min_length=8, max_length=1000)
    adapter: Literal['silicon-v1', 'tcp-red-v1', 'new-api-v1', 'spacex-hub-v1'] | None = None
    routing_group: RoutingGroup | None = None
    seller_user_id: str | None = Field(default=None, min_length=1, max_length=20, pattern=r'^[1-9][0-9]*$')
    token: str | None = Field(default=None, max_length=10000)
    enabled: bool | None = None
    collect_enabled: bool | None = None
    stats_config: dict | None = None


class SiteDelete(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: str


def site_json(row: Site, privileged=True):
    fields = ('id', 'display_id', 'name', 'health', 'enabled')
    if privileged:
        fields += ('prefix', 'base_url', 'adapter', 'routing_group', 'seller_user_id', 'token_hint', 'archived', 'stats_config',
                   'collect_enabled', 'verified_at', 'last_sync_at', 'created_at')
    result = {name: getattr(row, name) for name in fields}
    if privileged:
        cap = row.capabilities or {}
        state, diagnostic = verification_state(row)
        result['verified_version'] = safe_diagnostic('remote_error',
            observed_version=cap.get('verified_version')).get('observed_version')
        from ..adapters.newapi_builds import site_group_max_length, site_verified_build
        result['verified_build'] = site_verified_build(row) if state == 'verified' else None
        if row.adapter == 'spacex-hub-v1' and state == 'verified':
            from ..adapters.spacex_hub_build import (
                site_verified_build as spacex_verified_build,
            )
            result['verified_build'] = spacex_verified_build(row)
            notice = cap.get('create_block_reason')
            if isinstance(notice, str):
                result['adapter_notice'] = notice
        result['verification_error'] = diagnostic
        result['connection_check'] = connection_check_result(cap.get('connection_check'))
        options = {'models': [], 'groups': [], 'allow_custom_models': False,
                   'routing_group_max_length': site_group_max_length(row)}
        if state == 'verified':
            for field in ('models', 'groups'):
                values = cap.get(field)
                if isinstance(values, list):
                    options[field] = list(dict.fromkeys(value for value in values
                        if isinstance(value, str) and value.strip()))
            options['allow_custom_models'] = cap.get('custom_models') is True
        result['template_options'] = options
        result.update(distribution_readiness(object_session(row), row))
    return result


def find_site(db, site_id, *, lock=False):
    query = select(Site).where(Site.id == site_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if not row or row.archived:
        raise HTTPException(404, 'Site not found')
    return row


def verify_site(row: Site) -> bool:
    from ..adapters.silicon import RemoteError
    try:
        outcome = get_adapter(row).verify()
        row.capabilities = {**outcome['capabilities'], 'verified_version': outcome['verified_version']}
        row.health = 'healthy'
        row.verified_at = utcnow()
        return True
    except (RemoteError, ValueError, RuntimeError) as exc:
        category = exc.category if isinstance(exc, RemoteError) else 'configuration_error'
        diagnostic = safe_diagnostic(category, **{key: getattr(exc, key, None) for key in
            ('reason', 'observed_version', 'endpoint', 'method')}, http_status=getattr(exc, 'status_code', None))
        row.health = diagnostic['category']
        row.capabilities = {'verification_error': diagnostic}
        row.verified_at = None
        row.enabled = False
        return False


def normalize_url(value):
    try:
        return validate_remote_url(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def save(db):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Site prefix or address already exists') from exc


@router.get('')
def sites(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(Site).where(Site.archived.is_(False))
    if user.role != 'superadmin':
        query = query.where(Site.enabled.is_(True))
    rows = db.scalars(query.order_by(Site.display_id.asc())).all()
    items = [site_json(row, user.role == 'superadmin') for row in rows]
    if user.role == 'superadmin':
        from ..models_channels import Distribution, TaskItem
        for item in items:
            item['remote_channels'] = db.scalar(select(func.count()).select_from(Distribution).where(Distribution.site_id == item['id'], Distribution.remote_id.is_not(None)))
            item['unfinished_tasks'] = db.scalar(select(func.count()).select_from(TaskItem).where(TaskItem.site_id == item['id'], TaskItem.status.in_(['pending', 'running', 'unknown', 'retry'])))
    return {'items': items, 'total': len(items)}


@router.post('', status_code=201)
def create_site(body: SiteCreate, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    values = body.model_dump(exclude={'token', 'base_url', 'enabled'})
    row = Site(**values, base_url=normalize_url(body.base_url), token_encrypted=encrypt(body.token), token_hint=mask(body.token), enabled=False)
    success = verify_site(row)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Site prefix or address already exists') from exc
    row.enabled = bool(body.enabled and success and distribution_readiness(db, row)['distribution_ready'])
    audit(db, user, 'site.create', 'site', row.id, {'verified': success, 'enabled': row.enabled})
    save(db)
    return site_json(row)


@router.patch('/{site_id}')
def update_site(site_id: str, body: SitePatch, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = find_site(db, site_id, lock=True)
    values = body.model_dump(exclude_unset=True, exclude={'token', 'enabled'})
    if any(v is None for v in values.values()):
        raise HTTPException(422, 'Site fields cannot be null')
    if 'base_url' in values:
        values['base_url'] = normalize_url(values['base_url'])
        if values['base_url'] != row.base_url:
            from ..models_channels import Distribution, TaskItem
            if db.scalar(select(Distribution.id).where(Distribution.site_id == row.id).limit(1)) or db.scalar(select(TaskItem.id).where(TaskItem.site_id == row.id).limit(1)):
                raise HTTPException(409, 'A site with distribution history cannot change deployment address; register a new site')
    if 'seller_user_id' in values and values['seller_user_id'] != row.seller_user_id:
        from ..models_channels import Distribution, TaskItem
        # Failed, read-only initial sync attempts contain no distribution
        # identity. Allow correcting credentials after those checks, while
        # retaining the block for any channel history, active task or write.
        failed_initial_sync = and_(TaskItem.operation == 'sync', TaskItem.status == 'failed',
                                   TaskItem.stage == 'queued', TaskItem.remote_write_attempted.is_(False),
                                   TaskItem.channel_id.is_(None), TaskItem.distribution_id.is_(None))
        if db.scalar(select(Distribution.id).where(Distribution.site_id == row.id).limit(1)) or db.scalar(
                select(TaskItem.id).where(TaskItem.site_id == row.id, not_(failed_initial_sync)).limit(1)):
            raise HTTPException(409, 'A site with distribution history cannot change seller identity; register a new site')
    reverify = bool(body.token) or any(name in values and values[name] != getattr(row, name) for name in ('base_url', 'adapter', 'seller_user_id'))
    for name, value in values.items():
        setattr(row, name, value)
    if body.token:
        row.token_encrypted, row.token_hint = encrypt(body.token), mask(body.token)
    wanted_enabled = body.enabled if body.enabled is not None else row.enabled
    verified = verification_state(row)[0] == 'verified'
    if reverify or body.enabled is True or (wanted_enabled and not verified):
        success = verify_site(row) and verification_state(row)[0] == 'verified'
    else:
        success = verified
    db.flush()
    row.enabled = bool(wanted_enabled and success and distribution_readiness(db, row)['distribution_ready'])
    audit(db, user, 'site.update', 'site', row.id, {'fields': sorted(body.model_fields_set), 'enabled': row.enabled})
    save(db)
    return site_json(row)


@router.post('/{site_id}/verify')
def verify(site_id: str, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = find_site(db, site_id, lock=True)
    success = verify_site(row)
    db.flush()
    stop_unready_distribution(db, row, user)
    audit(db, user, 'site.verify', 'site', row.id, {'verified': success})
    db.commit()
    return site_json(row)


@router.post('/{site_id}/check-connection')
def check_connection(site_id: str, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    from ..adapters.silicon import RemoteError

    row = find_site(db, site_id, lock=True)
    try:
        outcome = get_adapter(row).check_connection()
        result = {'ok': True, 'version': outcome.get('version'), 'endpoints': outcome.get('endpoints')}
    except (RemoteError, ValueError, RuntimeError) as exc:
        category = exc.category if isinstance(exc, RemoteError) else 'configuration_error'
        diagnostic = safe_diagnostic(category, **{key: getattr(exc, key, None) for key in
            ('reason', 'observed_version', 'endpoint', 'method')}, http_status=getattr(exc, 'status_code', None))
        result = {'ok': False, 'error': diagnostic}
    result = connection_check_result({**result, 'checked_at': utcnow().isoformat()})
    # Connection checks do not grant capabilities, validate templates or toggle distribution.
    row.capabilities = {**(row.capabilities or {}), 'connection_check': result}
    audit(db, user, 'site.check_connection', 'site', row.id, {'ok': result['ok']})
    db.commit()
    return {'id': row.id, 'connection_check': result}


@router.delete('/{site_id}')
def delete_site(site_id: str, body: SiteDelete, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = find_site(db, site_id, lock=True)
    if body.confirmation != row.name:
        raise HTTPException(422, 'Enter the exact site name to confirm archival')
    # Always archive to preserve audit and all remote IDs. No remote deletion occurs.
    row.archived, row.enabled = True, False
    audit(db, user, 'site.archive', 'site', row.id)
    db.commit()
    return site_json(row)
