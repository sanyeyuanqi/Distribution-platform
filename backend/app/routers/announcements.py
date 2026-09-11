# FastAPI intentionally declares dependencies as default values.
# ruff: noqa: B008
import re
from datetime import UTC
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, require_roles
from ..db import get_db, utcnow
from ..models import Announcement, AnnouncementRead, Site, User
from ..security import decrypt

router = APIRouter(prefix='/announcements', tags=['Announcements'])
ROLES = {'superadmin', 'admin', 'user'}


class AnnouncementCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title_zh: str = Field(default='', max_length=200)
    content_zh: str = Field(default='', max_length=50000)
    title_en: str = Field(default='', max_length=200)
    content_en: str = Field(default='', max_length=50000)
    status: Literal['draft', 'published', 'withdrawn'] = 'draft'
    audience: list[str] = Field(default_factory=lambda: ['superadmin', 'admin', 'user'])

    @field_validator('audience', mode='before')
    @classmethod
    def normalize_audience(cls, value):
        if isinstance(value, str):
            value = sorted(ROLES) if value == 'all' else [value]
        if not isinstance(value, list) or not value or any(role not in ROLES for role in value):
            raise ValueError('Audience must contain valid roles')
        return sorted(set(value))


class AnnouncementPatch(AnnouncementCreate):
    pass


class AnnouncementDismiss(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, strict=True)


def beijing_today():
    return utcnow().replace(tzinfo=UTC).astimezone(ZoneInfo('Asia/Shanghai')).date()


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object', 'svg'):
            self.skip += 1
        if not self.skip and tag in ('p', 'br', 'div', 'li'):
            self.output.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe', 'object', 'svg'):
            self.skip = max(0, self.skip - 1)
        if not self.skip and tag in ('p', 'div', 'li'):
            self.output.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.output.append(data)


def sanitize(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    parser.close()
    return ''.join(parser.output).strip()


def ensure_content(db, row):
    values = [row.title_zh, row.content_zh, row.title_en, row.content_en]
    text = '\n'.join(values)
    if row.status == 'published' and (not row.title_zh.strip() or not row.content_zh.strip()):
        raise HTTPException(422, 'Published announcements require a Chinese title and content')
    if re.search(r'(?:\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY-----)', text):
        raise HTTPException(422, 'Announcements must not contain credentials')
    # Explicitly reject known site secrets even when they do not use an API-key prefix.
    for token in db.scalars(select(Site.token_encrypted)):
        secret = decrypt(token)
        if secret and secret in text:
            raise HTTPException(422, 'Announcements must not contain site credentials')
    # Match non-prefixed long credential candidates by keyed fingerprint without decrypting channels.
    from ..models_channels import Channel
    from ..security import fingerprint
    candidates = re.findall(r'[A-Za-z0-9_./+=:-]{20,}', text)
    if candidates and db.scalar(select(Channel.id).where(Channel.fingerprint.in_([fingerprint(c) for c in candidates])).limit(1)):
        raise HTTPException(422, 'Announcements must not contain channel credentials')


def announcement_json(row, read=False, management=False, *, dismissed_today=False):
    result = {name: getattr(row, name) for name in ('id', 'title_zh', 'content_zh', 'title_en', 'content_en', 'status', 'audience',
                                                  'version', 'publisher_id', 'published_at', 'created_at', 'updated_at')}
    result['unread'] = not read
    result['dismissed_today'] = dismissed_today
    result['title_en'] = row.title_en if management else (row.title_en or row.title_zh)
    result['content_en'] = row.content_en if management else (row.content_en or row.content_zh)
    result['english_fallback'] = not bool(row.title_en and row.content_en)
    if management:
        result['history'] = row.history
    return result


def visible(user, row):
    return user.role == 'superadmin' or (row.status == 'published' and user.role in row.audience)


@router.get('')
def announcements(user: User = Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    query = select(Announcement).order_by(Announcement.updated_at.desc())
    if user.role != 'superadmin':
        query = query.where(Announcement.status == 'published')
    reads = {(r.announcement_id, r.version): r for r in db.scalars(select(AnnouncementRead).where(AnnouncementRead.user_id == user.id))}
    rows = [r for r in db.scalars(query) if visible(user, r)]
    today = beijing_today()
    items = []
    for row in rows:
        state = reads.get((row.id, row.version))
        items.append(announcement_json(row, state is not None and state.read_at is not None,
            user.role == 'superadmin', dismissed_today=state is not None and state.dismissed_on == today))
    return {'items': items, 'total': len(items), 'today': today, 'timezone': 'Asia/Shanghai',
            'unread_count': sum(row['unread'] and row['status'] == 'published' and user.role in row['audience'] for row in items)}


@router.post('', status_code=201)
def create_announcement(body: AnnouncementCreate, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    values = body.model_dump()
    for name in ('title_zh', 'content_zh', 'title_en', 'content_en'):
        values[name] = sanitize(values[name])
    row = Announcement(**values, publisher_id=user.id, version=1, history=[])
    ensure_content(db, row)
    if row.status == 'published':
        row.published_at = utcnow()
    db.add(row)
    db.flush()
    audit(db, user, 'announcement.create', 'announcement', row.id, {'status': row.status, 'version': row.version})
    db.commit()
    return announcement_json(row, management=True)


@router.patch('/{announcement_id}')
def update_announcement(announcement_id: str, body: AnnouncementPatch, user: User = Depends(require_roles('superadmin')), db: Session = Depends(get_db)):
    row = db.scalar(select(Announcement).where(Announcement.id == announcement_id).with_for_update().execution_options(populate_existing=True))
    if not row:
        raise HTTPException(404, 'Announcement not found')
    previous = {name: getattr(row, name) for name in ('title_zh', 'content_zh', 'title_en', 'content_en', 'status', 'audience', 'version')}
    changed = False
    for name, value in body.model_dump(exclude_unset=True).items():
        if name in ('title_zh', 'content_zh', 'title_en', 'content_en'):
            value = sanitize(value)
        changed |= value != getattr(row, name)
        setattr(row, name, value)
    ensure_content(db, row)
    if changed:
        row.history = [*row.history, {**previous, 'saved_at': utcnow().isoformat(), 'actor_id': user.id}]
        row.version += 1
        row.updated_at = utcnow()
        row.publisher_id = user.id
        if row.status == 'published':
            row.published_at = utcnow()
        audit(db, user, 'announcement.update', 'announcement', row.id, {'version': row.version, 'status': row.status})
    db.commit()
    return announcement_json(row, management=True)


@router.post('/{announcement_id}/read')
def read_announcement(announcement_id: str, user: User = Depends(require_roles('superadmin', 'admin', 'user')), db: Session = Depends(get_db)):
    row = published_for_user(db, user, announcement_id)
    existing = read_state(db, user, row)
    if existing.read_at is None:
        existing.read_at = utcnow()
    db.commit()
    return {'ok': True, 'version': row.version}


def published_for_user(db, user, announcement_id):
    # Use the same parent lock as publishing: a stale dialog cannot dismiss a
    # version that was replaced or withdrawn while its request was in flight.
    row = db.scalar(select(Announcement).where(Announcement.id == announcement_id)
                    .with_for_update().execution_options(populate_existing=True))
    if not row or row.status != 'published' or user.role not in row.audience:
        raise HTTPException(404, 'Announcement not found')
    return row


def read_state(db, user, row):
    existing = db.scalar(select(AnnouncementRead).where(AnnouncementRead.user_id == user.id,
                           AnnouncementRead.announcement_id == row.id, AnnouncementRead.version == row.version))
    if not existing:
        existing = AnnouncementRead(user_id=user.id, announcement_id=row.id, version=row.version)
        db.add(existing)
    return existing


@router.post('/{announcement_id}/dismiss-today')
def dismiss_announcement_today(announcement_id: str, body: AnnouncementDismiss,
                              user: User = Depends(require_roles('superadmin', 'admin', 'user')),
                              db: Session = Depends(get_db)):
    row = published_for_user(db, user, announcement_id)
    if row.version != body.version:
        raise HTTPException(409, '公告已更新，请刷新后查看最新内容')
    existing = read_state(db, user, row)
    today = beijing_today()
    existing.dismissed_on = today
    db.commit()
    return {'ok': True, 'version': row.version, 'dismissed_on': today}
