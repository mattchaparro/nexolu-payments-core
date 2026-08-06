"""Lo que una app integradora CONSUME del Core para cobrar un pago.

Autenticado con la `api_key` de la integracion (`Authorization: Bearer
<api_key>`). Ver docs/APP_INTEGRATION.md para el flujo completo, incluido lo
que la app debe exponer de vuelta (el webhook saliente).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.auth.dependencies import get_current_integration
from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.memory.entities import Integration
from nexolu_payments_core.core.payments import service

router = APIRouter(prefix="/v1/payments", tags=["payments"])


class CustomerIn(BaseModel):
    email: str
    full_name: str = ""


class PaymentIntentIn(BaseModel):
    # La app integradora genera su propia reference (igual que hoy hace
    # pos-saas-legacy con "NEX-<business_id>-<timestamp>-<random>"): es lo
    # que usa para conciliar contra su propia orden/factura.
    reference: str = Field(min_length=3, max_length=128)
    amount_cop: int = Field(gt=0)
    currency: str = "COP"
    redirect_url: str
    customer: CustomerIn
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/intents", status_code=status.HTTP_201_CREATED)
async def create_intent(
    body: PaymentIntentIn,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        transaction, checkout = await service.create_payment_intent(
            session,
            integration=integration,
            reference=body.reference,
            amount_cop=body.amount_cop,
            currency=body.currency,
            redirect_url=body.redirect_url,
            customer=body.customer.model_dump(),
            metadata=body.metadata,
        )
    except service.DuplicateReference:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Ya existe una transaccion con esa reference."
        ) from None
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    await session.commit()

    return {
        "transaction_id": transaction.id,
        "reference": transaction.reference,
        "provider": transaction.provider_slug,
        "status": transaction.status,
        "checkout": checkout,
    }


@router.get("/transactions/{reference}")
async def get_transaction(
    reference: str,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    transaction = await repository.get_transaction_by_reference(session, integration.id, reference)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaccion no encontrada.")

    return {
        "transaction_id": transaction.id,
        "reference": transaction.reference,
        "provider": transaction.provider_slug,
        "status": transaction.status,
        "amount_cop": transaction.amount_cop,
        "currency": transaction.currency,
        "fee_cop": transaction.fee_cop,
        "net_amount_cop": transaction.net_amount_cop,
        "provider_transaction_id": transaction.provider_transaction_id,
        "created_at": transaction.created_at,
        "confirmed_at": transaction.confirmed_at,
    }
