from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, uid, utcnow


class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        CheckConstraint("role IN ('superadmin','admin','user')", name='ck_user_role'),
        CheckConstraint('display_id > 0', name='ck_user_display_id_positive'),
        UniqueConstraint('display_id', name='uq_users_display_id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True, start=1, minvalue=1, cycle=False))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(100), default='')
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Site(Base):
    __tablename__ = 'sites'
    __table_args__ = (
        CheckConstraint('display_id > 0', name='ck_site_display_id_positive'),
        UniqueConstraint('display_id', name='uq_sites_display_id'),
        Index('uq_sites_active_prefix', 'prefix', unique=True,
              postgresql_where=text('archived = false'), sqlite_where=text('archived = false')),
        Index('uq_sites_active_base_url', 'base_url', unique=True,
              postgresql_where=text('archived = false'), sqlite_where=text('archived = false')),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True, start=1, minvalue=1, cycle=False))
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(1000))
    adapter: Mapped[str] = mapped_column(String(80), default='silicon-v1')
    routing_group: Mapped[str] = mapped_column(String(160), default='default')
    seller_user_id: Mapped[str] = mapped_column(String(32))
    token_encrypted: Mapped[str] = mapped_column(Text)
    token_hint: Mapped[str] = mapped_column(String(50), default='••••')
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String(32), default='unverified')
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    stats_config: Mapped[dict] = mapped_column(JSON, default=dict)
    collect_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Category(Base):
    __tablename__ = 'categories'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    family: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CredentialFormat(Base):
    __tablename__ = 'credential_formats'
    __table_args__ = (UniqueConstraint('category_id', 'code', 'version', name='uq_format_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'), index=True)
    code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(32), default='1')
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    schema_config: Mapped[dict] = mapped_column(JSON, default=dict)
    default_models: Mapped[list] = mapped_column(JSON, default=list)


class SiteUploadTemplate(Base):
    __tablename__ = 'site_upload_templates'
    __table_args__ = (
        UniqueConstraint('site_id', 'category_id', 'variant', name='uq_upload_template_site_category_variant'),
        CheckConstraint('display_id > 0', name='ck_upload_template_display_id_positive'),
        UniqueConstraint('display_id', name='uq_upload_templates_display_id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True, start=1, minvalue=1, cycle=False))
    site_id: Mapped[str] = mapped_column(ForeignKey('sites.id'), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'), index=True)
    format_id: Mapped[str] = mapped_column(ForeignKey('credential_formats.id'))
    variant: Mapped[str] = mapped_column(String(32), default='', server_default='')
    name: Mapped[str] = mapped_column(String(120), default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    models: Mapped[list] = mapped_column(JSON, default=list)
    # Receiving demand only; never an upstream rate-limit configuration.
    model_rpm_requirements: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    model_tpm_requirements: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    model_mapping: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')
    routing_group: Mapped[str] = mapped_column(String(160), default='default')
    remark: Mapped[str] = mapped_column(Text, default='')
    channel_config: Mapped[dict] = mapped_column(JSON, default=dict)
    proxy_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), index=True, nullable=True)
    actor_role: Mapped[str] = mapped_column(String(16), default='anonymous')
    action: Mapped[str] = mapped_column(String(100), index=True)
    object_type: Mapped[str] = mapped_column(String(60))
    object_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    site_id: Mapped[str | None] = mapped_column(ForeignKey('sites.id'), nullable=True)
    result: Mapped[str] = mapped_column(String(32), default='success')
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Announcement(Base):
    __tablename__ = 'announcements'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title_zh: Mapped[str] = mapped_column(String(200))
    content_zh: Mapped[str] = mapped_column(Text)
    title_en: Mapped[str] = mapped_column(String(200), default='')
    content_en: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(16), default='draft')
    audience: Mapped[list] = mapped_column(JSON, default=lambda: ['superadmin', 'admin', 'user'])
    version: Mapped[int] = mapped_column(Integer, default=1)
    publisher_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    history: Mapped[list] = mapped_column(JSON, default=list)


class AnnouncementRead(Base):
    __tablename__ = 'announcement_reads'
    __table_args__ = (UniqueConstraint('user_id', 'announcement_id', 'version', name='uq_announcement_read'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    announcement_id: Mapped[str] = mapped_column(ForeignKey('announcements.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
