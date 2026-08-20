"""Public payment and trusted provisioning API."""
from __future__ import annotations

import secrets
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.config import get_settings
from nexolu_payments_core.core.auth.dependencies import get_current_integration
from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.memory.entities import Integration, Merchant, ProviderCredential
from nexolu_payments_core.core.payments import service
from nexolu_payments_core.core.security.api_keys import generate_secret, hash_api_key
from nexolu_payments_core.providers.base import BancolombiaTransferPaymentMethod, CardPaymentMethod, NequiPaymentMethod, PaymentMethodInput, PaymentSourceChargeMethod, ProviderRequestError, PsePaymentMethod

router = APIRouter(prefix="/v1", tags=["payments"])
provisioning_router = APIRouter(prefix="/admin", tags=["admin"])


def _require_provisioning_key(value: str | None) -> None:
    configured = get_settings().provisioning_key
    if not configured or not value or not secrets.compare_digest(value, configured):
        raise HTTPException(status_code=401, detail="Provisioning key invalida.")


class CustomerIn(BaseModel):
    email: str
    full_name: str = ""


class PaymentIntentIn(BaseModel):
    amount_cop: int = Field(gt=0)
    currency: str = Field(default="COP", min_length=3, max_length=8)
    redirect_url: str = ""
    customer: CustomerIn
    metadata: dict[str, Any] = Field(default_factory=dict)
    flow: Literal["widget", "api"] = "widget"


class CardPaymentMethodIn(BaseModel):
    type: Literal["CARD"] = "CARD"
    token: str
    installments: int = Field(default=1, ge=1)


class NequiPaymentMethodIn(BaseModel):
    type: Literal["NEQUI"] = "NEQUI"
    phone_number: str = Field(pattern=r"^3\d{9}$")


class PsePaymentMethodIn(BaseModel):
    type: Literal["PSE"] = "PSE"
    user_type: int = Field(ge=0, le=1)
    user_legal_id_type: str
    user_legal_id: str
    financial_institution_code: str
    payment_description: str = Field(max_length=64)
    customer_full_name: str
    customer_phone_number: str


class BancolombiaTransferPaymentMethodIn(BaseModel):
    type: Literal["BANCOLOMBIA_TRANSFER"] = "BANCOLOMBIA_TRANSFER"
    payment_description: str = Field(max_length=64)
    ecommerce_url: str


class PaymentSourceChargeMethodIn(BaseModel):
    type: Literal["PAYMENT_SOURCE"] = "PAYMENT_SOURCE"
    payment_source_id: str
    installments: int = Field(default=1, ge=1)


PaymentMethodIn = Annotated[Union[CardPaymentMethodIn, NequiPaymentMethodIn, PsePaymentMethodIn, BancolombiaTransferPaymentMethodIn, PaymentSourceChargeMethodIn], Field(discriminator="type")]


class ChargeIn(BaseModel):
    payment_method: PaymentMethodIn


def _to_payment_method(value: PaymentMethodIn) -> PaymentMethodInput:
    if isinstance(value, CardPaymentMethodIn):
        return CardPaymentMethod(token=value.token, installments=value.installments)
    if isinstance(value, NequiPaymentMethodIn):
        return NequiPaymentMethod(phone_number=value.phone_number)
    if isinstance(value, PsePaymentMethodIn):
        return PsePaymentMethod(user_type=value.user_type, user_legal_id_type=value.user_legal_id_type, user_legal_id=value.user_legal_id, financial_institution_code=value.financial_institution_code, payment_description=value.payment_description, customer_full_name=value.customer_full_name, customer_phone_number=value.customer_phone_number)
    if isinstance(value, BancolombiaTransferPaymentMethodIn):
        return BancolombiaTransferPaymentMethod(payment_description=value.payment_description, ecommerce_url=value.ecommerce_url)
    return PaymentSourceChargeMethod(payment_source_id=value.payment_source_id, installments=value.installments)


class CreatePaymentSourceIn(BaseModel):
    type: Literal["CARD", "NEQUI"]
    token: str
    customer_email: str


@router.post("/payments/intents", status_code=201, summary="Create a payment intent")
async def create_intent(body: PaymentIntentIn, integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        transaction, checkout, payment_init = await service.create_payment_intent(session, integration=integration, amount_cop=body.amount_cop, currency=body.currency, redirect_url=body.redirect_url, customer=body.customer.model_dump(), metadata=body.metadata, flow=body.flow)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo preparar el pago con el proveedor: {exc}") from exc
    await session.commit()
    result: dict[str, Any] = {"transaction_id": transaction.id, "reference": transaction.reference, "provider": transaction.provider_slug, "status": transaction.status, "checkout": checkout}
    if payment_init is not None:
        result["payment_init"] = payment_init
    return result


@router.post("/payments/intents/{reference}/charge", summary="Charge a payment intent")
async def charge_intent(reference: str, body: ChargeIn, integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        transaction, result = await service.charge_payment_intent(session, integration=integration, reference=reference, payment_method=_to_payment_method(body.payment_method))
    except service.TransactionNotChargeable:
        raise HTTPException(status_code=404, detail="No existe una transaccion pendiente para esa reference.") from None
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo iniciar el cobro con el proveedor: {exc}") from exc
    return {"transaction_id": transaction.id, "reference": transaction.reference, "status": transaction.status, "provider_transaction_id": result.provider_transaction_id, "provider_status": result.raw_status, "redirect_url": result.redirect_url}


@router.get("/payments/payment-methods", summary="Available payment methods")
async def list_payment_methods(integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        methods = await service.get_available_payment_methods(session, integration=integration)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"provider": "wompi", "accepted_payment_methods": methods, "widget_enabled": integration.widget_enabled}


@router.get("/payments/pse/financial-institutions", summary="PSE financial institutions")
async def list_pse_financial_institutions(integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        institutions = await service.get_pse_financial_institutions(session, integration=integration)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"financial_institutions": [{"code": x.code, "name": x.name} for x in institutions]}


@router.post("/payments/payment-sources", status_code=201, summary="Create a reusable payment source")
async def create_payment_source(body: CreatePaymentSourceIn, integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        source = await service.create_payment_source(session, integration=integration, source_type=body.type, token=body.token, customer_email=body.customer_email)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"payment_source_id": source.id, "type": source.type, "status": source.status}


@router.put("/payments/payment-sources/{payment_source_id}/void", summary="Void a payment source")
async def void_payment_source(payment_source_id: str, integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    try:
        source = await service.void_payment_source(session, integration=integration, payment_source_id=payment_source_id)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"payment_source_id": source.id, "type": source.type, "status": source.status}


@router.get("/payments/transactions/{reference}", summary="Get transaction status")
async def get_transaction(reference: str, integration: Integration = Depends(get_current_integration), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    transaction = await repository.get_transaction_by_reference(session, reference)
    if transaction is None or transaction.integration_id != integration.id:
        raise HTTPException(status_code=404, detail="Transaccion no encontrada.")
    return {"transaction_id": transaction.id, "reference": transaction.reference, "provider": transaction.provider_slug, "status": transaction.status, "amount_cop": transaction.amount_cop, "currency": transaction.currency, "fee_cop": transaction.fee_cop, "net_amount_cop": transaction.net_amount_cop, "provider_transaction_id": transaction.provider_transaction_id, "created_at": transaction.created_at, "confirmed_at": transaction.confirmed_at}


class MerchantIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


class IntegrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    environment: str = Field(default="sandbox", pattern=r"^(sandbox|production)$")
    webhook_url: str | None = None
    widget_enabled: bool = False


class IntegrationUpdateIn(BaseModel):
    widget_enabled: bool | None = None
    webhook_url: str | None = None
    is_active: bool | None = None


class WompiCredentialsIn(BaseModel):
    environment: str = Field(default="sandbox", pattern=r"^(sandbox|production)$")
    public_key: str
    private_key: str
    integrity_secret: str
    events_secret: str


@provisioning_router.get("/merchants", summary="List merchants")
async def list_merchants(x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    merchants = await repository.list_merchants(session)
    return {"merchants": [{"id": m.id, "name": m.name, "slug": m.slug, "is_active": m.is_active} for m in merchants]}


@provisioning_router.post("/merchants", status_code=201, summary="Create a merchant")
async def create_merchant(body: MerchantIn, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    if await repository.get_merchant_by_slug(session, body.slug):
        raise HTTPException(status_code=409, detail="El merchant ya existe.")
    merchant = Merchant(name=body.name, slug=body.slug)
    session.add(merchant)
    await session.flush()
    await session.commit()
    return {"id": merchant.id, "name": merchant.name, "slug": merchant.slug, "is_active": merchant.is_active}


@provisioning_router.get("/merchants/{merchant_id}", summary="Get merchant")
async def get_merchant(merchant_id: str, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    return {"id": merchant.id, "name": merchant.name, "slug": merchant.slug, "is_active": merchant.is_active}


@provisioning_router.post("/merchants/{merchant_id}/integrations", status_code=201, summary="Create an integration")
async def create_integration(merchant_id: str, body: IntegrationIn, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    if await repository.get_integration_by_slug(session, body.slug):
        raise HTTPException(status_code=409, detail="La integration ya existe.")
    integration = Integration(merchant_id=merchant.id, name=body.name, slug=body.slug, environment=body.environment, webhook_url=body.webhook_url, widget_enabled=body.widget_enabled)
    session.add(integration)
    await session.flush()
    await session.commit()
    return {"id": integration.id, "merchant_id": integration.merchant_id, "name": integration.name, "slug": integration.slug, "environment": integration.environment, "webhook_url": integration.webhook_url, "widget_enabled": integration.widget_enabled, "is_active": integration.is_active, "api_key": integration.api_key, "webhook_secret": integration.webhook_secret}


@provisioning_router.get("/merchants/{merchant_id}/integrations", summary="List integrations for a merchant")
async def list_integrations(merchant_id: str, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    integrations = await repository.list_integrations_by_merchant(session, merchant_id)
    return {"integrations": [{"id": i.id, "merchant_id": i.merchant_id, "name": i.name, "slug": i.slug, "environment": i.environment, "webhook_url": i.webhook_url, "widget_enabled": i.widget_enabled, "is_active": i.is_active} for i in integrations]}


@provisioning_router.get("/merchants/{merchant_id}/integrations/{integration_id}", summary="Get an integration")
async def get_integration(merchant_id: str, integration_id: str, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    # api_key/webhook_secret nunca se re-exponen aca (solo en la respuesta
    # de create_integration, una unica vez) - mismo criterio que
    # get_wompi_status con private_key/integrity_secret/events_secret.
    _require_provisioning_key(x_payments_provisioning_key)
    integration = await repository.get_integration_by_id(session, integration_id)
    if integration is None or integration.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Integration no encontrada.")
    return {"id": integration.id, "merchant_id": integration.merchant_id, "name": integration.name, "slug": integration.slug, "environment": integration.environment, "webhook_url": integration.webhook_url, "widget_enabled": integration.widget_enabled, "is_active": integration.is_active}


@provisioning_router.patch("/merchants/{merchant_id}/integrations/{integration_id}", summary="Update an integration")
async def update_integration(merchant_id: str, integration_id: str, body: IntegrationUpdateIn, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    integration = await repository.get_integration_by_id(session, integration_id)
    if integration is None or integration.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Integration no encontrada.")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(integration, field, value)
    await session.commit()
    return {"id": integration.id, "merchant_id": integration.merchant_id, "name": integration.name, "slug": integration.slug, "environment": integration.environment, "webhook_url": integration.webhook_url, "widget_enabled": integration.widget_enabled, "is_active": integration.is_active}


@provisioning_router.post("/merchants/{merchant_id}/integrations/{integration_id}/regenerate-secret", summary="Regenerate api_key and webhook_secret")
async def regenerate_integration_secret(merchant_id: str, integration_id: str, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    # Invalida el api_key/webhook_secret viejos de una - cualquier app
    # cliente que todavia los tenga guardados empieza a fallar auth de
    # inmediato (mismo criterio que create_integration: los secretos nuevos
    # solo se ven una vez, en esta respuesta).
    _require_provisioning_key(x_payments_provisioning_key)
    integration = await repository.get_integration_by_id(session, integration_id)
    if integration is None or integration.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Integration no encontrada.")
    new_api_key = generate_secret("nxl")
    integration.api_key = new_api_key
    integration.api_key_hash = hash_api_key(new_api_key)
    integration.webhook_secret = generate_secret("whsec")
    await session.commit()
    return {"id": integration.id, "merchant_id": integration.merchant_id, "api_key": integration.api_key, "webhook_secret": integration.webhook_secret}


@provisioning_router.delete("/merchants/{merchant_id}/integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Deactivate an integration")
async def delete_integration(merchant_id: str, integration_id: str, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> None:
    # Soft-delete (is_active=False), no DELETE de la fila: una Integration
    # con transacciones asociadas no se puede borrar de verdad sin romper
    # la integridad referencial (transactions.integration_id, FK no
    # nullable) - y aun sin transacciones, preferimos conservar el
    # historial. La Integration deja de aceptar auth (ver
    # repository.get_integration_by_api_key, que ya filtra is_active).
    _require_provisioning_key(x_payments_provisioning_key)
    integration = await repository.get_integration_by_id(session, integration_id)
    if integration is None or integration.merchant_id != merchant_id:
        raise HTTPException(status_code=404, detail="Integration no encontrada.")
    integration.is_active = False
    await session.commit()


@provisioning_router.post("/merchants/{merchant_id}/providers/wompi", status_code=201, summary="Configure Wompi credentials")
async def configure_wompi(merchant_id: str, body: WompiCredentialsIn, x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    merchant = await repository.get_merchant_by_id(session, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant no encontrado.")
    if await repository.get_active_credential(session, merchant.id, "wompi", body.environment):
        raise HTTPException(status_code=409, detail="Ya existe una credencial Wompi activa para este merchant y entorno.")
    credential = ProviderCredential(merchant_id=merchant.id, provider_slug="wompi", environment=body.environment, public_key=body.public_key, private_key=body.private_key, integrity_secret=body.integrity_secret, events_secret=body.events_secret)
    session.add(credential)
    await session.flush()
    await session.commit()
    return {"id": credential.id, "merchant_id": merchant.id, "provider": "wompi", "environment": credential.environment, "configured": True}


@provisioning_router.get("/merchants/{merchant_id}/providers/wompi", summary="Get Wompi configuration status")
async def get_wompi_status(merchant_id: str, environment: str = "sandbox", x_payments_provisioning_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _require_provisioning_key(x_payments_provisioning_key)
    credential = await repository.get_active_credential(session, merchant_id, "wompi", environment)
    return {"provider": "wompi", "environment": environment, "configured": credential is not None, "public_key": credential.public_key if credential else None}


router.include_router(provisioning_router)
