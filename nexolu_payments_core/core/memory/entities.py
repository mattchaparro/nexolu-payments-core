"""Modelos de persistencia del Core.

Deliberadamente NO hay tablas de negocio aca (sin `usuarios`, sin
`suscripciones`): eso vive en la base de datos de cada app integradora. Lo
que el Core persiste es lo que le pertenece a la pasarela misma: que apps
estan conectadas, con que credenciales de proveedor y que comision, el
historial de transacciones que proceso, y el log de lo que le notifico a
cada app.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexolu_payments_core.core.memory.db import Base
from nexolu_payments_core.core.security.crypto import EncryptedString


def _uuid() -> str:
    return uuid.uuid4().hex


class Integration(Base):
    """Una app cliente de la pasarela (pos-saas-legacy, nexolu-ia-core...).

    El Core no sabe nada del negocio de esa app: solo necesita saber con que
    `api_key` se identifica y a que `webhook_url` notificarle los cambios de
    estado de sus transacciones, firmados con su propio `webhook_secret`
    (analogo al par public/events-secret que ya usa Wompi, pero en el sentido
    Core -> app integradora).
    """

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(16), default="sandbox")  # sandbox|production
    api_key: Mapped[str] = mapped_column(String(64), unique=True)
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    webhook_secret: Mapped[str] = mapped_column(EncryptedString(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    provider_credentials: Mapped[list[ProviderCredential]] = relationship(back_populates="integration")
    fee_schedules: Mapped[list[FeeSchedule]] = relationship(back_populates="integration")


class ProviderCredential(Base):
    """Credenciales de UN proveedor (hoy solo Wompi) para UNA integracion, en
    UN entorno. Cada app puede tener su propio merchant account de Wompi: el
    Core no asume una cuenta compartida como hace hoy pos-saas-legacy."""

    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "provider_slug", "environment", name="uq_credential_integration_provider_env"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    provider_slug: Mapped[str] = mapped_column(String(32), default="wompi")
    environment: Mapped[str] = mapped_column(String(16), default="sandbox")
    # El public_key de Wompi viaja al frontend del cliente para abrir el
    # widget de checkout: no es secreto, se guarda en claro. Los demas si.
    public_key: Mapped[str] = mapped_column(String(255))
    private_key: Mapped[str] = mapped_column(EncryptedString(255))
    integrity_secret: Mapped[str] = mapped_column(EncryptedString(255))
    events_secret: Mapped[str] = mapped_column(EncryptedString(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    integration: Mapped[Integration] = relationship(back_populates="provider_credentials")


class FeeSchedule(Base):
    """Comision retenida por el proveedor, versionada por integracion.

    Misma formula que `WompiFees` del POS legacy (porcentaje + fijo, IVA
    sobre ese subtotal) pero parametrizada por `integration_id` en vez de
    global: cada app puede negociar su propia tarifa con Wompi. Versionada
    (varias filas por integracion, `is_active` marca la vigente) para que
    cambiar una tarifa no reescriba el fee ya calculado de transacciones
    pasadas.
    """

    __tablename__ = "fee_schedules"
    __table_args__ = (Index("ix_fee_schedules_integration", "integration_id", "provider_slug", "is_active"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    provider_slug: Mapped[str] = mapped_column(String(32), default="wompi")
    percent_fee: Mapped[float] = mapped_column(Float, default=2.65)
    fixed_fee_cop: Mapped[int] = mapped_column(Integer, default=700)
    iva_percent: Mapped[float] = mapped_column(Float, default=19.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    integration: Mapped[Integration] = relationship(back_populates="fee_schedules")


class Transaction(Base):
    """Registro central de verdad de cada pago procesado. `reference` es el
    identificador que la app integradora genero al iniciar el pago (no un id
    interno del proveedor) -- es lo que la app usa para hacer seguimiento, y
    lo unico que viaja de vuelta en el webhook saliente junto al resultado."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("integration_id", "reference", name="uq_transaction_reference_per_integration"),
        Index("ix_transactions_integration_status", "integration_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    provider_slug: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(128))
    provider_transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_cop: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="COP")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|declined|error|voided
    fee_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_amount_cop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebhookDelivery(Base):
    """Auditoria y reintentos de las notificaciones que el Core le manda a
    cada app integradora cuando una de sus transacciones cambia de estado."""

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
