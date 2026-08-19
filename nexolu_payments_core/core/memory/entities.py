"""Persistence models for Nexolu Payments Core."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexolu_payments_core.core.memory.db import Base
from nexolu_payments_core.core.security.api_keys import hash_api_key
from nexolu_payments_core.core.security.crypto import EncryptedString


def _uuid() -> str:
    return uuid.uuid4().hex


def _secret(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    integrations: Mapped[list[Integration]] = relationship(back_populates="merchant")
    provider_credentials: Mapped[list[ProviderCredential]] = relationship(back_populates="merchant")
    fee_schedules: Mapped[list[FeeSchedule]] = relationship(back_populates="merchant")


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(16), default="sandbox")
    api_key: Mapped[str] = mapped_column(EncryptedString(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    webhook_secret: Mapped[str] = mapped_column(EncryptedString(255), default=lambda: _secret("whsec"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    merchant: Mapped[Merchant] = relationship(back_populates="integrations")
    fee_schedules: Mapped[list[FeeSchedule]] = relationship(back_populates="integration")

    def __init__(self, **kwargs):
        api_key = kwargs.pop("api_key", None) or _secret("nxl")
        self.api_key = api_key
        self.api_key_hash = hash_api_key(api_key)
        super().__init__(**kwargs)


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (UniqueConstraint("merchant_id", "provider_slug", "environment", name="uq_credential_merchant_provider_env"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    provider_slug: Mapped[str] = mapped_column(String(32), default="wompi")
    environment: Mapped[str] = mapped_column(String(16), default="sandbox")
    public_key: Mapped[str] = mapped_column(String(255))
    private_key: Mapped[str] = mapped_column(EncryptedString(255))
    integrity_secret: Mapped[str] = mapped_column(EncryptedString(255))
    events_secret: Mapped[str] = mapped_column(EncryptedString(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    merchant: Mapped[Merchant] = relationship(back_populates="provider_credentials")


class FeeSchedule(Base):
    __tablename__ = "fee_schedules"
    __table_args__ = (Index("ix_fee_schedules_merchant", "merchant_id", "provider_slug", "is_active"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    integration_id: Mapped[str | None] = mapped_column(ForeignKey("integrations.id"), nullable=True, index=True)
    provider_slug: Mapped[str] = mapped_column(String(32), default="wompi")
    percent_fee: Mapped[float] = mapped_column(Float, default=2.65)
    fixed_fee_cop: Mapped[int] = mapped_column(Integer, default=700)
    iva_percent: Mapped[float] = mapped_column(Float, default=19.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    merchant: Mapped[Merchant] = relationship(back_populates="fee_schedules")
    integration: Mapped[Integration | None] = relationship(back_populates="fee_schedules")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_transaction_reference"),
        Index("ix_transactions_reference", "reference"),
        Index("ix_transactions_merchant_status", "merchant_id", "status"),
        Index("ix_transactions_integration_status", "integration_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), index=True)
    provider_slug: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(128), unique=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_cop: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="COP")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # Solo lo necesita flow="api" para metodos asincronos (PSE, Boton
    # Bancolombia): hay que reenviarselo a Wompi al cobrar (POST
    # /transactions) para que redirija de vuelta a la app cuando el usuario
    # termina en el sitio del banco - sin esto Wompi no sabe a donde volver.
    # El flujo Widget no lo necesita (el redirect lo maneja el propio
    # widget.js client-side), pero se guarda para cualquier flow igual.
    redirect_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fee_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_amount_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_transaction", "transaction_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"))
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    event: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
