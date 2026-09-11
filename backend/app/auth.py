# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008
import hmac
import json
import secrets
import time
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from redis import Redis, RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuditEvent, User

COOKIE_NAME = 'keyacross_session'


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)


def redis_call(method: str, *args, **kwargs):
    try:
        return getattr(get_redis(), method)(*args, **kwargs)
    except RedisError as exc:
        raise HTTPException(503, 'Session service unavailable') from exc


def create_session(user: User) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    data = {'user_id': user.id, 'version': user.session_version, 'created': time.time(), 'csrf': csrf}
    redis_call('set', f'session:{token}', json.dumps(data), ex=min(settings.session_idle_seconds, settings.session_max_seconds))
    return token, csrf


def limit_authenticated_request(user_id: str, method: str, path: str):
    """Independent server-side buckets; Redis errors fail closed for protected actions."""
    buckets = []
    if method.upper() not in ('GET', 'HEAD', 'OPTIONS'):
        buckets.append(('write', 120))
    if path.rstrip('/').endswith('/reveal'):
        buckets.append(('reveal', 20))
    if method.upper() in ('GET', 'HEAD') and path.rstrip('/').endswith('/export'):
        buckets.append(('download', 60))
    window = int(time.time()) // 60
    for bucket, limit in buckets:
        key = f'rate:{bucket}:{user_id}:{window}'
        count = redis_call('incr', key)
        if count == 1:
            redis_call('expire', key, 90)
        if count > limit:
            raise HTTPException(429, 'Request rate limit exceeded; retry later',
                                headers={'Retry-After': str(60 - int(time.time()) % 60)})


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 128:
        raise HTTPException(401, 'Authentication required')
    raw = redis_call('get', f'session:{token}')
    try:
        data = json.loads(raw) if raw else {}
        remaining = int(settings.session_max_seconds - (time.time() - float(data['created'])))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, 'Session expired')
    user = db.get(User, data.get('user_id'))
    if remaining <= 0 or not user or not user.active or user.archived or user.session_version != data.get('version'):
        redis_call('delete', f'session:{token}')
        raise HTTPException(401, 'Session expired')
    if request.method.upper() not in ('GET', 'HEAD', 'OPTIONS'):
        provided = request.headers.get('X-CSRF-Token', '')
        if not provided or not hmac.compare_digest(provided, data.get('csrf', '')):
            raise HTTPException(403, 'Invalid CSRF token')
        validate_origin(request)
    limit_authenticated_request(user.id, request.method, request.url.path)
    request.state.csrf_token = data['csrf']
    request.state.session_token = token
    redis_call('expire', f'session:{token}', min(remaining, settings.session_idle_seconds))
    return user


def validate_origin(request: Request):
    origin = request.headers.get('origin')
    expected = {x.strip().rstrip('/') for x in settings.cors_origins.split(',') if x.strip()}
    expected.add(str(request.base_url).rstrip('/'))
    if origin and origin.rstrip('/') not in expected:
        raise HTTPException(403, 'Untrusted request origin')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-site requests are not allowed')


def require_roles(*roles):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, 'Insufficient permissions')
        return user
    return dependency


def scope_owner_ids(db: Session, user: User, scope: str = 'team', owner_id: str | None = None) -> list[str]:
    if scope not in ('mine', 'team'):
        raise HTTPException(422, 'Invalid scope')
    if user.role == 'superadmin':
        allowed = list(db.scalars(select(User.id)))
    elif user.role == 'admin':
        allowed = [user.id, *db.scalars(select(User.id).where(User.parent_id == user.id, User.role == 'user'))]
    else:
        allowed = [user.id]
    if owner_id:
        if owner_id not in allowed:
            raise HTTPException(404, 'User not found')
        return [owner_id]
    return [user.id] if scope == 'mine' else allowed


def assert_owner(db: Session, user: User, owner_id: str):
    scope_owner_ids(db, user, owner_id=owner_id)


_SENSITIVE_KEYS = ('password', 'token', 'secret', 'credential', 'key_encrypted', 'proxy', 'recipient_account')


def _redact(value):
    if isinstance(value, dict):
        return {str(k): '[redacted]' if str(k).lower() == 'key' or any(s in str(k).lower() for s in _SENSITIVE_KEYS) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(x) for x in value[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


def audit(db: Session, user: User | None, action: str, object_type: str, object_id: str | None, summary=None):
    row = AuditEvent(actor_id=user.id if user else None, actor_role=user.role if user else 'anonymous',
                     action=action, object_type=object_type, object_id=object_id,
                     summary=_redact(summary if isinstance(summary, dict) else {'message': summary} if summary else {}))
    db.add(row)
    return row


def user_json(user: User) -> dict:
    return {name: getattr(user, name) for name in ('id', 'display_id', 'username', 'nickname', 'role', 'parent_id', 'active', 'archived', 'created_at')}
