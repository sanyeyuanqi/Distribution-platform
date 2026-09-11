# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    COOKIE_NAME,
    audit,
    create_session,
    get_current_user,
    redis_call,
    user_json,
    validate_origin,
)
from ..config import settings
from ..db import get_db
from ..models import User
from ..security import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    hash_password,
    verify_password,
)

router = APIRouter(prefix='/auth', tags=['Authentication'])
_dummy_hash = hash_password('unused-dummy-password-for-timing')


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nickname: str | None = Field(default=None, min_length=1, max_length=100)
    current_password: str | None = Field(default=None, max_length=PASSWORD_MAX_LENGTH)
    password: str | None = Field(default=None, min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


def _session_response(response: Response, user: User):
    token, csrf = create_session(user)
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_max_seconds, httponly=True,
                        secure=settings.cookie_secure, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return {'user': user_json(user), 'csrf_token': csrf}


@router.post('/login')
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_origin(request)
    ip = request.client.host if request.client else 'unknown'
    # Separate IP and account windows stop distributed account guessing and name sprays.
    keys = [('login:ip:' + hashlib.sha256(ip.encode()).hexdigest(), 60),
            ('login:account:' + hashlib.sha256(body.username.lower().encode()).hexdigest(), 10)]
    for key, limit in keys:
        count = redis_call('incr', key)
        if count == 1:
            redis_call('expire', key, 900)
        if count > limit:
            raise HTTPException(429, '登录尝试过于频繁，请稍后重试', headers={'Retry-After': '900'})
    user = db.scalar(select(User).where(User.username == body.username))
    valid_password = verify_password(body.password, user.password_hash if user else _dummy_hash)
    if not valid_password or not user or not user.active or user.archived:
        event = audit(db, user, 'auth.login.failed', 'user', user.id if user else None)
        event.result = 'denied'
        db.commit()
        raise HTTPException(401, '用户名或密码错误')
    old = request.cookies.get(COOKIE_NAME)
    if old:
        redis_call('delete', f'session:{old}')
    result = _session_response(response, user)
    audit(db, user, 'auth.login', 'user', user.id)
    db.commit()
    redis_call('delete', keys[1][0])
    return result


@router.get('/me')
def me(request: Request, response: Response, user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'no-store'
    return {'user': user_json(user), 'csrf_token': request.state.csrf_token}


@router.post('/logout')
def logout(request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    redis_call('delete', f'session:{request.state.session_token}')
    response.delete_cookie(COOKIE_NAME, path='/', secure=settings.cookie_secure, httponly=True, samesite='lax')
    audit(db, user, 'auth.logout', 'user', user.id)
    db.commit()
    return {'ok': True}


@router.patch('/profile')
def profile(body: Profile, request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.password:
        user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
        if not body.current_password or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(400, '当前密码错误')
        user.password_hash = hash_password(body.password)
        user.session_version += 1
        redis_call('delete', f'session:{request.state.session_token}')
    if body.nickname is not None:
        user.nickname = body.nickname
    audit(db, user, 'auth.profile.update', 'user', user.id,
          {'nickname_changed': body.nickname is not None, 'password_changed': bool(body.password)})
    db.commit()
    if body.password:
        return _session_response(response, user)
    return {'user': user_json(user), 'csrf_token': request.state.csrf_token}
