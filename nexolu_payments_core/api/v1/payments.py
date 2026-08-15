"""Lo que una app integradora CONSUME del Core para cobrar un pago.

Autenticado con la `api_key` de la integracion (`Authorization: Bearer
<api_key>`). Ver docs/APP_INTEGRATION.md para el flujo completo, incluido lo
que la app debe exponer de vuelta (el webhook saliente).

Dos formas de completar el cobro (`POST /intents` con `flow`):

- `flow="widget"` (default, legado): la respuesta trae `checkout` -- la app
  abre el widget hospedado por el proveedor con esos parametros.
- `flow="api"`: la respuesta trae ademas `payment_init` -- el frontend de la
  app tokeniza la tarjeta hablando DIRECTO con el proveedor (nunca con el
  Core) usando `payment_init.public_key`, y luego la app llama
  `POST /intents/{reference}/charge` con el token resultante.

En ambos casos el resultado final de la transaccion llega por
`POST /v1/webhooks/wompi/<slug>` (ver `api/v1/webhooks.py`), nunca por lo
que devuelve el navegador -- `GET /transactions/{reference}` sirve para
hacer polling mientras tanto.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.auth.dependencies import get_current_integration
from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.memory.entities import Integration
from nexolu_payments_core.core.payments import service
from nexolu_payments_core.providers.base import (
    BancolombiaTransferPaymentMethod,
    CardPaymentMethod,
    NequiPaymentMethod,
    PaymentMethodInput,
    PaymentSourceChargeMethod,
    ProviderRequestError,
    PsePaymentMethod,
)

router = APIRouter(prefix="/v1/payments", tags=["payments"])


class CustomerIn(BaseModel):
    email: str = Field(description="Correo del cliente final (Wompi lo usa para el comprobante).")
    full_name: str = Field(default="", description="Nombre del cliente final.")


class PaymentIntentIn(BaseModel):
    # La app integradora genera su propia reference (igual que hoy hace
    # pos-saas-legacy con "NEX-<business_id>-<timestamp>-<random>"): es lo
    # que usa para conciliar contra su propia orden/factura.
    reference: str = Field(
        min_length=3,
        max_length=128,
        description="Identificador unico tuyo para esta transaccion (tu orden/factura).",
    )
    amount_cop: int = Field(gt=0, description="Monto a cobrar en pesos colombianos (no en centavos).")
    currency: str = Field(default="COP", description="Por ahora solo se soporta COP.")
    redirect_url: str = Field(description="A donde redirigir al usuario tras pagar (solo aplica al flujo Widget).")
    customer: CustomerIn
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Datos tuyos (ids internos, dias de suscripcion...); el Core no los interpreta, "
            "solo te los reenvia en el webhook."
        ),
    )
    flow: Literal["widget", "api"] = Field(
        default="widget",
        description=(
            "'widget' (default, legado): la respuesta trae `checkout` para abrir el widget del proveedor. "
            "'api': la respuesta trae ademas `payment_init` para completar el cobro sin salir de tu app, "
            "ver POST /intents/{reference}/charge."
        ),
    )


class CardPaymentMethodIn(BaseModel):
    type: Literal["CARD"] = "CARD"
    token: str = Field(
        description=(
            "Token de tarjeta que TU FRONTEND obtuvo hablando directo con el proveedor "
            "(usando `payment_init.public_key` del intent) -- el Core nunca recibe el numero de tarjeta."
        )
    )
    installments: int = Field(default=1, ge=1, description="Numero de cuotas.")


class NequiPaymentMethodIn(BaseModel):
    type: Literal["NEQUI"] = "NEQUI"
    phone_number: str = Field(
        pattern=r"^3\d{9}$",
        description="Celular colombiano de 10 digitos registrado con Nequi.",
    )


class PsePaymentMethodIn(BaseModel):
    type: Literal["PSE"] = "PSE"
    user_type: int = Field(ge=0, le=1, description="0 = persona natural, 1 = persona juridica.")
    user_legal_id_type: str = Field(description="Tipo de documento del pagador (ej. CC, NIT).")
    user_legal_id: str = Field(description="Numero de documento del pagador.")
    financial_institution_code: str = Field(
        description="Codigo del banco elegido, de GET /v1/payments/pse/financial-institutions."
    )
    payment_description: str = Field(max_length=64, description="Descripcion del pago, sin comillas simples.")
    customer_full_name: str = Field(description="Nombre completo del pagador.")
    customer_phone_number: str = Field(description="Telefono del pagador.")


class BancolombiaTransferPaymentMethodIn(BaseModel):
    type: Literal["BANCOLOMBIA_TRANSFER"] = "BANCOLOMBIA_TRANSFER"
    payment_description: str = Field(max_length=64, description="Descripcion del pago, sin comillas simples.")
    ecommerce_url: str = Field(description="A donde te redirige Wompi tras completar el pago en Bancolombia.")


class PaymentSourceChargeMethodIn(BaseModel):
    type: Literal["PAYMENT_SOURCE"] = "PAYMENT_SOURCE"
    payment_source_id: str = Field(description="Id devuelto por POST /payment-sources.")
    installments: int = Field(default=1, ge=1, description="Solo aplica si la fuente es una tarjeta.")


PaymentMethodIn = Annotated[
    Union[
        CardPaymentMethodIn,
        NequiPaymentMethodIn,
        PsePaymentMethodIn,
        BancolombiaTransferPaymentMethodIn,
        PaymentSourceChargeMethodIn,
    ],
    Field(discriminator="type"),
]


class ChargeIn(BaseModel):
    payment_method: PaymentMethodIn


def _to_payment_method(payment_method_in: PaymentMethodIn) -> PaymentMethodInput:
    if isinstance(payment_method_in, CardPaymentMethodIn):
        return CardPaymentMethod(token=payment_method_in.token, installments=payment_method_in.installments)
    if isinstance(payment_method_in, NequiPaymentMethodIn):
        return NequiPaymentMethod(phone_number=payment_method_in.phone_number)
    if isinstance(payment_method_in, PsePaymentMethodIn):
        return PsePaymentMethod(
            user_type=payment_method_in.user_type,
            user_legal_id_type=payment_method_in.user_legal_id_type,
            user_legal_id=payment_method_in.user_legal_id,
            financial_institution_code=payment_method_in.financial_institution_code,
            payment_description=payment_method_in.payment_description,
            customer_full_name=payment_method_in.customer_full_name,
            customer_phone_number=payment_method_in.customer_phone_number,
        )
    if isinstance(payment_method_in, BancolombiaTransferPaymentMethodIn):
        return BancolombiaTransferPaymentMethod(
            payment_description=payment_method_in.payment_description,
            ecommerce_url=payment_method_in.ecommerce_url,
        )
    return PaymentSourceChargeMethod(
        payment_source_id=payment_method_in.payment_source_id,
        installments=payment_method_in.installments,
    )


class CreatePaymentSourceIn(BaseModel):
    type: Literal["CARD", "NEQUI"] = Field(
        description="Unicos dos tipos que Wompi permite tokenizar para reuso (ver docs/APP_INTEGRATION.md)."
    )
    token: str = Field(
        description=(
            "Token ya obtenido por TU FRONTEND hablando directo con el proveedor "
            "(POST /tokens/cards o /tokens/nequi con la public_key -- nunca con el Core). "
            "Para Nequi, debe estar en status APPROVED (el usuario ya acepto la suscripcion en su celular)."
        )
    )
    customer_email: str = Field(description="Correo del cliente dueno de la fuente de pago.")


@router.post(
    "/intents",
    status_code=status.HTTP_201_CREATED,
    summary="Crear un intent de pago",
    response_description="El intent creado, con los parametros de checkout del flujo elegido.",
)
async def create_intent(
    body: PaymentIntentIn,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        transaction, checkout, payment_init = await service.create_payment_intent(
            session,
            integration=integration,
            reference=body.reference,
            amount_cop=body.amount_cop,
            currency=body.currency,
            redirect_url=body.redirect_url,
            customer=body.customer.model_dump(),
            metadata=body.metadata,
            flow=body.flow,
        )
    except service.DuplicateReference:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Ya existe una transaccion con esa reference."
        ) from None
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo preparar el pago con el proveedor: {exc}",
        ) from exc

    await session.commit()

    out: dict[str, Any] = {
        "transaction_id": transaction.id,
        "reference": transaction.reference,
        "provider": transaction.provider_slug,
        "status": transaction.status,
        "checkout": checkout,
    }
    # Solo se agrega si se pidio flow="api" -- para flow="widget" (default)
    # la respuesta es identica, campo por campo, a la de antes de este
    # cambio (ver docs/APP_INTEGRATION.md, seccion de migracion).
    if payment_init is not None:
        out["payment_init"] = payment_init

    return out


@router.post(
    "/intents/{reference}/charge",
    summary="Cobrar un intent con una tarjeta ya tokenizada (API directa)",
    response_description="Ack inmediato del proveedor. El estado final llega por webhook.",
)
async def charge_intent(
    reference: str,
    body: ChargeIn,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payment_method = _to_payment_method(body.payment_method)

    try:
        transaction, result = await service.charge_payment_intent(
            session,
            integration=integration,
            reference=reference,
            payment_method=payment_method,
        )
    except service.TransactionNotChargeable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No hay una transaccion pendiente con esa reference "
                "(el intent se creo con flow='api'?, ya se cobro antes?)."
            ),
        ) from None
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        # El proveedor nunca acepto el intento de cobro: no va a haber
        # webhook para este reference. `charge_payment_intent` ya dejo la
        # transaccion en `error` -- el contrato del Core no se rompe, solo
        # se le informa a la app que el proveedor fallo.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo iniciar el cobro con el proveedor: {exc}",
        ) from exc

    return {
        "transaction_id": transaction.id,
        "reference": transaction.reference,
        # Se queda "pending" a proposito: la confirmacion real llega por
        # webhook, ver GET /transactions/{reference} para hacer polling.
        "status": transaction.status,
        "provider_transaction_id": result.provider_transaction_id,
        "provider_status": result.raw_status,
        # Solo poblado para PSE/BANCOLOMBIA_TRANSFER: a donde redirigir al
        # usuario para que termine el pago en el sitio de su banco. `None`
        # para CARD/NEQUI, o si el proveedor no lo entrego a tiempo (ver
        # providers/wompi.py) -- en ese caso seguir esperando el webhook.
        "redirect_url": result.redirect_url,
    }


@router.get(
    "/payment-methods",
    summary="Metodos de pago disponibles",
    response_description="Los que el proveedor tiene habilitados, filtrados a lo que este Core sabe orquestar.",
)
async def list_payment_methods(
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        methods = await service.get_available_payment_methods(session, integration=integration)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo consultar los metodos de pago disponibles: {exc}",
        ) from exc

    return {"provider": "wompi", "accepted_payment_methods": methods}


@router.get(
    "/pse/financial-institutions",
    summary="Bancos disponibles para pagar por PSE",
)
async def list_pse_financial_institutions(
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        institutions = await service.get_pse_financial_institutions(session, integration=integration)
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo consultar los bancos PSE: {exc}",
        ) from exc

    return {
        "financial_institutions": [
            {"code": institution.code, "name": institution.name} for institution in institutions
        ]
    }


@router.post(
    "/payment-sources",
    status_code=status.HTTP_201_CREATED,
    summary="Guardar una tarjeta o cuenta Nequi para reuso (Fuentes de Pago)",
    response_description="El payment_source_id, para reusar en POST /intents/{reference}/charge.",
)
async def create_payment_source(
    body: CreatePaymentSourceIn,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        source = await service.create_payment_source(
            session,
            integration=integration,
            source_type=body.type,
            token=body.token,
            customer_email=body.customer_email,
        )
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo crear la fuente de pago con el proveedor: {exc}",
        ) from exc

    return {"payment_source_id": source.id, "type": source.type, "status": source.status}


@router.put(
    "/payment-sources/{payment_source_id}/void",
    summary="Cancelar una fuente de pago guardada",
)
async def void_payment_source(
    payment_source_id: str,
    integration: Integration = Depends(get_current_integration),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        source = await service.void_payment_source(
            session, integration=integration, payment_source_id=payment_source_id
        )
    except service.IntegrationNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo cancelar la fuente de pago con el proveedor: {exc}",
        ) from exc

    return {"payment_source_id": source.id, "type": source.type, "status": source.status}


@router.get(
    "/transactions/{reference}",
    summary="Consultar el estado de una transaccion",
)
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
