"""Persistence queries shared by auth, provisioning and payment services."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory.entities import (
    FeeSchedule,
    Integration,
    Merchant,
    ProviderCredential,
    Transaction,
    WebhookDelivery,
)
from nexolu_payments_core.core.security.api_keys import hash_api_key


async def get_merchant_by_id(session: AsyncSession, merchant_id: str) -> Merchant | None:
    return (await session.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()


async def get_merchant_by_slug(session: AsyncSession, slug: str) -> Merchant | None:
    return (await session.execute(select(Merchant).where(Merchant.slug == slug))).scalar_one_or_none()


async def list_merchants(session: AsyncSession) -> list[Merchant]:
    stmt = select(Merchant).order_by(Merchant.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_integration_by_api_key(session: AsyncSession, api_key: str) -> Integration | None:
    stmt = select(Integration).join(Merchant, Merchant.id == Integration.merchant_id).where(
        Integration.api_key_hash == hash_api_key(api_key),
        Integration.is_active.is_(True),
        Merchant.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_integration_by_id(session: AsyncSession, integration_id: str) -> Integration | None:
    return (await session.execute(select(Integration).where(Integration.id == integration_id))).scalar_one_or_none()


async def get_integration_by_slug(session: AsyncSession, slug: str) -> Integration | None:
    return (await session.execute(select(Integration).where(Integration.slug == slug, Integration.is_active.is_(True)))).scalar_one_or_none()


async def list_integrations_by_merchant(session: AsyncSession, merchant_id: str) -> list[Integration]:
    stmt = select(Integration).where(Integration.merchant_id == merchant_id).order_by(Integration.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


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


async def get_transaction_by_id(session: AsyncSession, transaction_id: str) -> Transaction | None:
    return (await session.execute(select(Transaction).where(Transaction.id == transaction_id))).scalar_one_or_none()


async def list_transactions(
    session: AsyncSession,
    *,
    merchant_id: str | None = None,
    status: str | None = None,
    provider_slug: str | None = None,
    reference: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Transaction, str, str, str, str]]:
    """Cada fila: (Transaction, merchant_name, merchant_slug, integration_name,
    integration_slug) - joineados aca para que el panel de administracion no
    tenga que resolver nombres a partir de ids el mismo (N+1 del lado del
    BFF)."""
    stmt = (
        select(Transaction, Merchant.name, Merchant.slug, Integration.name, Integration.slug)
        .join(Merchant, Merchant.id == Transaction.merchant_id)
        .join(Integration, Integration.id == Transaction.integration_id)
        .order_by(Transaction.created_at.desc())
    )
    if merchant_id:
        stmt = stmt.where(Transaction.merchant_id == merchant_id)
    if status:
        stmt = stmt.where(Transaction.status == status)
    if provider_slug:
        stmt = stmt.where(Transaction.provider_slug == provider_slug)
    if reference:
        stmt = stmt.where(Transaction.reference.contains(reference))
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return [(t, mn, ms, iname, islug) for t, mn, ms, iname, islug in result.all()]


async def list_webhook_deliveries_by_transaction_ids(
    session: AsyncSession, transaction_ids: list[str]
) -> list[WebhookDelivery]:
    if not transaction_ids:
        return []
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.transaction_id.in_(transaction_ids))
        .order_by(WebhookDelivery.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())
