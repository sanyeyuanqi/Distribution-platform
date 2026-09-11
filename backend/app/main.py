from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal
from .security import PASSWORD_TOO_LONG, PASSWORD_TOO_SHORT


@asynccontextmanager
async def lifespan(app):
    from .security import encrypt
    # Fail startup on missing/invalid separately-managed encryption material.
    encrypt('startup-validation')
    if len(settings.secret_key) < 32:
        raise RuntimeError('SECRET_KEY must contain at least 32 characters')
    yield


app = FastAPI(title='KeyAcross API', version='1.0.0', description='多平台渠道管理 / 用户权限 / 订单结算', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[s.strip() for s in settings.cors_origins.split(',') if s.strip()],
    allow_credentials=True, allow_methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'],
    allow_headers=['Content-Type', 'X-CSRF-Token', 'Idempotency-Key'])


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={'detail': '记录已存在或关联数据冲突，请刷新后重试'})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic input/context may contain passwords, seller tokens or raw credentials.
    def message(error):
        if error['loc'][-1:] not in [('password',), ('current_password',)]:
            return error['msg']
        if error['type'] == 'string_too_short':
            return '不能为空' if error.get('ctx', {}).get('min_length') == 1 else PASSWORD_TOO_SHORT
        if error['type'] == 'string_too_long':
            return PASSWORD_TOO_LONG
        if error['type'] == 'missing':
            return '不能为空'
        if error['type'] == 'string_type':
            return '必须是文本'
        # Only fixed, known messages may leave a password validator; never echo input/context.
        if error['type'] == 'value_error':
            for known in (PASSWORD_TOO_SHORT, PASSWORD_TOO_LONG):
                if error['msg'] == f'Value error, {known}':
                    return known
        return '格式不正确'

    return JSONResponse(status_code=422, content={'detail': [
        {'loc': list(e['loc']), 'msg': message(e), 'type': e['type']} for e in exc.errors()]})


@app.get('/api/health', tags=['health'])
def health():
    from .auth import get_redis
    try:
        with SessionLocal() as db:
            db.execute(text('SELECT 1'))
        get_redis().ping()
    except Exception as exc:
        raise HTTPException(503, 'Database or Redis is unavailable') from exc
    return {'status': 'ok', 'database': 'ok', 'redis': 'ok', 'version': '1.0.0'}


from .routers import (
    announcements,
    audit,
    auth,
    billing,
    catalog,
    channels,
    settlement_orders,
    sites,
    stats,
    tasks,
    upload_templates,
    uploads,
    users,
)

for module in (auth, users, sites, catalog, announcements, audit, uploads, upload_templates, channels, tasks,
               billing, settlement_orders, stats):
    app.include_router(module.router, prefix='/api')
