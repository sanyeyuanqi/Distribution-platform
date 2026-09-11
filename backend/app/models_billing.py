from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, uid, utcnow

Money = Numeric(28, 8)


class UsageFact(Base):
    __tablename__ = 'usage_facts'
    __table_args__ = (UniqueConstraint('site_id', 'distribution_id', 'source_id', name='uq_usage_source'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(String(200))
    site_id: Mapped[str] = mapped_column(ForeignKey('sites.id'), index=True)
    distribution_id: Mapped[str] = mapped_column(ForeignKey('distributions.id'), index=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    admin_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'), index=True)
    service_variant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[object] = mapped_column(DateTime, index=True)
    period_end: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    raw_amount: Mapped[Decimal] = mapped_column(Money)
    raw_unit: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    unit: Mapped[str] = mapped_column(String(40), default='USD')
    conversion_version: Mapped[str] = mapped_column(String(120))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_kind: Mapped[str] = mapped_column(String(40), default='verified_import')
    source_revision: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class DiscountVersion(Base):
    __tablename__ = 'discount_versions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    payer_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    payee_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'), index=True)
    service_variant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    inherits_category: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    layer: Mapped[str] = mapped_column(String(16))
    percent: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    effective_at: Mapped[object] = mapped_column(DateTime)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class SettlementOrder(Base):
    __tablename__ = 'settlement_orders'
    __table_args__ = (
        UniqueConstraint('actor_id', 'idempotency_key', name='uq_settlement_order_request'),
        CheckConstraint("status = 'settled'", name='ck_settlement_order_status'),
        CheckConstraint('usage_amount >= 0 AND payment_amount >= 0', name='ck_settlement_order_amounts'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    number: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default='settled')
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    actor_name: Mapped[str] = mapped_column(String(160))
    payer_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    payer_name: Mapped[str] = mapped_column(String(160))
    payee_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    payee_name: Mapped[str] = mapped_column(String(160))
    identity_snapshot: Mapped[dict] = mapped_column(JSON)
    layer: Mapped[str] = mapped_column(String(16), index=True)
    # PostgreSQL unconstrained NUMERIC preserves the source counter precision.
    usage_amount: Mapped[Decimal] = mapped_column(Numeric())
    payment_amount: Mapped[Decimal] = mapped_column(Numeric())
    pricing_unit: Mapped[str] = mapped_column(String(16), default='USD')
    payment_unit: Mapped[str] = mapped_column(String(16), default='USDT')
    line_count: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, index=True)


class SettlementOrderLine(Base):
    __tablename__ = 'settlement_order_lines'
    __table_args__ = (
        UniqueConstraint('order_id', 'channel_id', name='uq_settlement_order_channel'),
        CheckConstraint('discount_percent >= 0 AND discount_percent <= 100', name='ck_settlement_order_discount'),
        CheckConstraint('usage_amount >= 0 AND payment_amount >= 0', name='ck_settlement_order_line_amounts'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('settlement_orders.id'), index=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'), index=True)
    display_id: Mapped[int] = mapped_column(BigInteger)
    group_id: Mapped[str] = mapped_column(ForeignKey('upload_groups.id'))
    group_tag: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    owner_name: Mapped[str] = mapped_column(String(160))
    owner_username: Mapped[str] = mapped_column(String(64))
    category_id: Mapped[str] = mapped_column(ForeignKey('categories.id'))
    category_name: Mapped[str] = mapped_column(String(100))
    variant: Mapped[str] = mapped_column(String(32))
    service_name: Mapped[str] = mapped_column(String(160))
    service_name_en: Mapped[str] = mapped_column(String(160))
    discount_id: Mapped[str | None] = mapped_column(ForeignKey('discount_versions.id'), nullable=True)
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    usage_amount: Mapped[Decimal] = mapped_column(Numeric())
    payment_amount: Mapped[Decimal] = mapped_column(Numeric())
    # Per-target evidence remains the baseline for incremental settlement.
    site_amounts: Mapped[list] = mapped_column(JSON)
