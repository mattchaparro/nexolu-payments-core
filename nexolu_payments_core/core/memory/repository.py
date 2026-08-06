"""Consultas de persistencia reusadas por el servicio de pagos y por auth.

Deliberadamente funciones sueltas (no una clase Repository con estado): cada
una recibe la sesion de la request, sin sesion propia guardada -- asi no hay
ambiguedad sobre en que transaccion corre cada consulta.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory.entities import (
    FeeSchedule,
    Integration,
    ProviderCredential,
    Transaction,
)


async def get_integration_by_api_key(session: AsyncSession, api_key: str) -> Integration | None:
    stmt = select(Integration).where(Integration.api_key == api_key, Integration.is_active.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_integration_by_slug(session: AsyncSession, slug: str) -> Integration | None:
    stmt = select(Integration).where(Integration.slug == slug, Integration.is_active.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_active_credential(
    session: AsyncSession, integration_id: str, provider_slug: str
) -> ProviderCredential | None:
    stmt = (
        select(ProviderCredential)
        .where(
            ProviderCredential.integration_id == integration_id,
            ProviderCredential.provider_slug == provider_slug,
            ProviderCredential.is_active.is_(True),
        )
        .order_by(ProviderCredential.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().first()


async def get_active_fee_schedule(
    session: AsyncSession, integration_id: str, provider_slug: str
) -> FeeSchedule | None:
    stmt = (
        select(FeeSchedule)
        .where(
            FeeSchedule.integration_id == integration_id,
            FeeSchedule.provider_slug == provider_slug,
            FeeSchedule.is_active.is_(True),
        )
        .order_by(FeeSchedule.effective_from.desc())
    )
    return (await session.execute(stmt)).scalars().first()


async def get_transaction_by_reference(
    session: AsyncSession, integration_id: str, reference: str
) -> Transaction | None:
    stmt = select(Transaction).where(
        Transaction.integration_id == integration_id, Transaction.reference == reference
    )
    return (await session.execute(stmt)).scalar_one_or_none()
