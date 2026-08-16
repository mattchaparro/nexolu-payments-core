"""Persistence queries shared by auth, provisioning and payment services."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory.entities import FeeSchedule, Integration, Merchant, ProviderCredential, Transaction


async def get_merchant_by_id(session: AsyncSession, merchant_id: str) -> Merchant | None:
    return (await session.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()


async def get_merchant_by_slug(session: AsyncSession, slug: str) -> Merchant | None:
    return (await session.execute(select(Merchant).where(Merchant.slug == slug))).scalar_one_or_none()


async def get_integration_by_api_key(session: AsyncSession, api_key: str) -> Integration | None:
    stmt = select(Integration).join(Merchant, Merchant.id == Integration.merchant_id).where(
        Integration.api_key == api_key,
        Integration.is_active.is_(True),
        Merchant.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_integration_by_id(session: AsyncSession, integration_id: str) -> Integration | None:
    return (await session.execute(select(Integration).where(Integration.id == integration_id))).scalar_one_or_none()


async def get_integration_by_slug(session: AsyncSession, slug: str) -> Integration | None:
    stmt = select(Integration).where(Integration.slug == slug, Integration.is_active.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_active_credential(session: AsyncSession, merchant_id: str, provider_slug: str, environment: str = "sandbox") -> ProviderCredential | None:
    stmt = select(ProviderCredential).where(
        ProviderCredential.merchant_id == merchant_id,
        ProviderCredential.provider_slug == provider_slug,
        ProviderCredential.environment == environment,
        ProviderCredential.is_active.is_(True),
    ).order_by(ProviderCredential.created_at.desc())
    return (await session.execute(stmt)).scalars().first()


async def get_active_fee_schedule(session: AsyncSession, merchant_id: str, provider_slug: str) -> FeeSchedule | None:
    stmt = select(FeeSchedule).where(
        FeeSchedule.merchant_id == merchant_id,
        FeeSchedule.provider_slug == provider_slug,
        FeeSchedule.is_active.is_(True),
    ).order_by(FeeSchedule.effective_from.desc())
    return (await session.execute(stmt)).scalars().first()


async def get_transaction_by_reference(session: AsyncSession, reference: str) -> Transaction | None:
    return (await session.execute(select(Transaction).where(Transaction.reference == reference))).scalar_one_or_none()
