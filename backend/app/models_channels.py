"""Durable channel ownership, credential history and remote work ledger."""
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, uid, utcnow


class UploadGroup(Base):
    __tablename__ = 'upload_groups'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'))
    format_id: Mapped[str] = mapped_column(ForeignKey('credential_formats.id'))
    tag: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160), default='')
    remark: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class Channel(Base):
    __tablename__ = 'channels'
    __table_args__ = (
        UniqueConstraint('owner_id', 'fingerprint', name='uq_channel_owner_fingerprint'),
        CheckConstraint('display_id > 0', name='ck_channel_display_id_positive'),
        UniqueConstraint('display_id', name='uq_channels_display_id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    display_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True, start=1, minvalue=1, cycle=False))
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey('upload_groups.id'), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'), index=True)
    format_id: Mapped[str] = mapped_column(ForeignKey('credential_formats.id'))
    key_encrypted: Mapped[str] = mapped_column(Text)
    key_hint: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    key_mode: Mapped[str] = mapped_column(String(16), default='single', server_default='single')
    key_count: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    models: Mapped[list] = mapped_column(JSON, default=list)
    remark: Mapped[str] = mapped_column(Text, default='')
    declaration: Mapped[str] = mapped_column(Text, default='')
    upload_mode: Mapped[str] = mapped_column(String(16), default='advanced')
    upload_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    proxy_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class KeyVersion(Base):
    __tablename__ = 'key_versions'
    __table_args__ = (UniqueConstraint('channel_id', 'version', name='uq_key_channel_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    key_encrypted: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128))
    valid_from: Mapped[object] = mapped_column(DateTime, default=utcnow)
    valid_to: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class ChannelCredential(Base):
    """Current member identities, never credentials; versions remain encrypted."""
    __tablename__ = 'channel_credentials'
    __table_args__ = (UniqueConstraint('owner_id', 'fingerprint', name='uq_member_owner_fingerprint'),
                     UniqueConstraint('channel_id', 'ordinal', name='uq_member_channel_ordinal'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)


class Distribution(Base):
    __tablename__ = 'distributions'
    __table_args__ = (UniqueConstraint('channel_id', 'site_id', 'partition_key', name='uq_distribution_channel_site_partition'),
                     UniqueConstraint('site_id', 'remote_id', name='uq_distribution_site_remote'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'), index=True)
    site_id: Mapped[str] = mapped_column(ForeignKey('sites.id'), index=True)
    remote_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    partition_key: Mapped[str] = mapped_column(String(64), default='', server_default='')
    partition_label: Mapped[str] = mapped_column(String(160), default='', server_default='')
    key_count: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    status: Mapped[str] = mapped_column(String(40), default='pending')
    remote_name: Mapped[str] = mapped_column(String(160))
    models: Mapped[list] = mapped_column(JSON, default=list)
    routing_group: Mapped[str] = mapped_column(String(160), default='default')
    # Historical identity intentionally survives hard deletion of a template.
    upload_template_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    test_status: Mapped[str] = mapped_column(String(40), default='unsupported')
    tested_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    usage_status: Mapped[str] = mapped_column(String(40), default='unsupported')
    last_sync_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    adopted_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    adoption_baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class DistributionVersion(Base):
    __tablename__ = 'distribution_versions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    distribution_id: Mapped[str] = mapped_column(ForeignKey('distributions.id'), index=True)
    key_version: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[object] = mapped_column(DateTime, default=utcnow)
    valid_to: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class Task(Base):
    __tablename__ = 'tasks'
    __table_args__ = (UniqueConstraint('actor_id', 'idempotency_key', name='uq_task_actor_idempotency'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    execution_actor_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    actor_session_version: Mapped[int] = mapped_column(Integer)
    group_id: Mapped[str | None] = mapped_column(ForeignKey('upload_groups.id'), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default='queued')
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class TaskItem(Base):
    __tablename__ = 'task_items'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id'), index=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey('channels.id'), nullable=True, index=True)
    site_id: Mapped[str] = mapped_column(ForeignKey('sites.id'), index=True)
    distribution_id: Mapped[str | None] = mapped_column(ForeignKey('distributions.id'), nullable=True)
    operation: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default='pending', index=True)
    stage: Mapped[str] = mapped_column(String(60), default='queued')
    key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # Frozen ciphertext allows readback after the source template is changed/deleted.
    proxy_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    remote_write_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class UnclaimedChannel(Base):
    __tablename__ = 'unclaimed_channels'
    __table_args__ = (UniqueConstraint('site_id', 'remote_id', name='uq_unclaimed_site_remote'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    site_id: Mapped[str] = mapped_column(ForeignKey('sites.id'), index=True)
    remote_id: Mapped[str] = mapped_column(String(80))
    remote_name: Mapped[str] = mapped_column(String(160))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    adopted_channel_id: Mapped[str | None] = mapped_column(ForeignKey('channels.id'), nullable=True)
    discovered_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
