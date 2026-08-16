"""Provisioning API for merchants, integrations and provider credentials.

These endpoints are intentionally protected by a server-side provisioning key.
A future management frontend should call them through its authenticated
backend/session layer rather than exposing this key to a browser.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.config import get_settings
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.memory.entities import Integration, Merchant, ProviderCredential
from nexolu_payments_core.core.memory import repository

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def require_admin_key(x_payments_admin_key: str | None = Header(default=None)) -> None:
    configured = get_settings().provisioning_key
    if not configured or not x_payments_admin_key or not secrets.compare_digest(x_payments_admin_key, configured):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Provisioning key invalida.")


class MerchantIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


class IntegrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    environment: str = Field(default="sandbox", pattern=r"^(sandbox|production)$")
    webhook_url: str | None = None


class WompiCredentialsIn(BaseModel):
    environment: str = Field(default="sandbox", pattern=r"^(sandbox|production)$")
    public_key: str
    private_key: str
    integrity_secret: str
    events_secret: str


@router.post("/merchants", status_code=201, dependencies=[Depends(require_admin_key)])
async def create_merchant(body: MerchantIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if await repository.get_merchant_by_slug(session, body.slug):
        raise HTTPException(status_code=409, detail="El merchant ya existe.")
    merchant = Merchant(name=body.name, slug=body.slug)
    session.add(merchant)
    await session.flush()
    await session.commit()
    return {"id": merchant.id, "name": merchant.name, "slug": merchant.slug, "is_active": merchant.is_active}


@router.get("/merchants/{merchant_id}", dependencies=[Depends(require_admin_key)])
async def get_merchant(merchant_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    return {"id": merchant.id, "name": merchant.name, "slug": merchant.slug, "is_active": merchant.is_active}


@router.post("/merchants/{merchant_id}/integrations", status_code=201, dependencies=[Depends(require_admin_key)])
async def create_integration(merchant_id: str, body: IntegrationIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    if await repository.get_integration_by_slug(session, body.slug):
        raise HTTPException(status_code=409, detail="La integration ya existe.")

    integration = Integration(merchant_id=merchant.id, name=body.name, slug=body.slug, environment=body.environment, webhook_url=body.webhook_url)
    session.add(integration)
    await session.flush()
    await session.commit()

    # API key and webhook secret are returned only at creation time.
    return {"id": integration.id, "merchant_id": integration.merchant_id, "name": integration.name, "slug": integration.slug, "environment": integration.environment, "api_key": integration.api_key, "webhook_secret": integration.webhook_secret}


@router.post("/merchants/{merchant_id}/providers/wompi", status_code=201, dependencies=[Depends(require_admin_key)])
async def configure_wompi(merchant_id: str, body: WompiCredentialsIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")

    existing = await repository.get_active_credential(session, merchant.id, "wompi", body.environment)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una credencial Wompi activa para este merchant y entorno.")

    credential = ProviderCredential(merchant_id=merchant.id, provider_slug="wompi", environment=body.environment, public_key=body.public_key, private_key=body.private_key, integrity_secret=body.integrity_secret, events_secret=body.events_secret)
    session.add(credential)
    await session.flush()
    await session.commit()
    return {"id": credential.id, "merchant_id": merchant.id, "provider": "wompi", "environment": credential.environment, "configured": True}


@router.get("/merchants/{merchant_id}/providers/wompi", dependencies=[Depends(require_admin_key)])
async def get_wompi_status(merchant_id: str, environment: str = "sandbox", session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    credential = await repository.get_active_credential(session, merchant_id, "wompi", environment)
    return {"provider": "wompi", "environment": environment, "configured": credential is not None, "public_key": credential.public_key if credential else None}
