"""Orquestador de pagos: une proveedor + persistencia + comision + webhook
saliente. Los endpoints HTTP (`api/v1/payments.py`, `api/v1/webhooks.py`) son
delgados a proposito -- toda la logica vive aca para poder probarla sin
levantar la app.

Dos formas de completar un pago conviven (ver docs/APP_INTEGRATION.md):

- **Widget (legado)**: `create_payment_intent` arma los parametros de
  checkout y la app abre el widget del proveedor; el resultado llega
  siempre por webhook (`handle_provider_webhook`).
- **API directa**: `create_payment_intent(..., flow="api")` arma en su
  lugar `payment_init` (tokens de aceptacion + firma) para que el frontend
  tokenice la tarjeta hablando directo con el proveedor; despues
  `charge_payment_intent` le pide al Core que intente cobrar esa tarjeta ya
  tokenizada. El resultado FINAL sigue llegando siempre por webhook -- lo
  que devuelve `charge_payment_intent` es solo el ack inmediato del
  proveedor (ver `ProviderRequestError`/`ChargeResult` en
  `providers/base.py`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.entities import Integration, ProviderCredential, Transaction
from nexolu_payments_core.core.payments.fees import calculate_fee_cop
from nexolu_payments_core.core.webhooks.dispatcher import dispatch_transaction_event
from nexolu_payments_core.providers.base import (
    ChargeResult,
    FinancialInstitution,
    PaymentMethodInput,
    PaymentSource,
    ProviderCredentialsData,
    ProviderRequestError,
)
from nexolu_payments_core.providers.registry import get_provider

_EVENT_BY_KIND = {
    "approved": "payment.approved",
    "declined": "payment.declined",
    "error": "payment.error",
    "voided": "payment.voided",
}


class IntegrationNotConfigured(Exception):
    """La integracion no tiene credenciales activas para el proveedor pedido."""


class DuplicateReference(Exception):
    """Ya existe una transaccion con esa `reference` para esta integracion."""


class TransactionNotChargeable(Exception):
    """No existe una transaccion `pending` con esa `reference` para esta
    integracion -- o nunca se creo el intent, o ya se intento/confirmo un
    cobro para ella (no es idempotente reintentar un charge)."""


def _credentials_data(credential: ProviderCredential) -> ProviderCredentialsData:
    return ProviderCredentialsData(
        public_key=credential.public_key,
        private_key=credential.private_key,
        integrity_secret=credential.integrity_secret,
        events_secret=credential.events_secret,
    )


async def create_payment_intent(
    session: AsyncSession,
    *,
    integration: Integration,
    reference: str,
    amount_cop: int,
    currency: str,
    redirect_url: str,
    customer: dict[str, Any],
    metadata: dict[str, Any],
    provider_slug: str = "wompi",
    flow: str = "widget",
) -> tuple[Transaction, dict[str, Any], dict[str, Any] | None]:
    if await repository.get_transaction_by_reference(session, integration.id, reference):
        raise DuplicateReference(reference)

    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    credentials = _credentials_data(credential)

    checkout = provider.build_checkout(
        reference=reference,
        amount_cop=amount_cop,
        currency=currency,
        credentials=credentials,
        redirect_url=redirect_url,
        customer=customer,
    )

    # `payment_init` (API directa) requiere una llamada de red al proveedor
    # (tokens de aceptacion legal) -- se pide ANTES de crear la fila de
    # Transaction para no dejar un intent "pending" huerfano en el Core si
    # el proveedor no responde.
    payment_init_out: dict[str, Any] | None = None
    if flow == "api":
        payment_init = await provider.build_payment_init(
            reference=reference,
            amount_cop=amount_cop,
            currency=currency,
            credentials=credentials,
        )
        payment_init_out = {
            "public_key": payment_init.public_key,
            "amount_in_cents": payment_init.amount_in_cents,
            "currency": payment_init.currency,
            "reference": payment_init.reference,
            "integrity_signature": payment_init.integrity_signature,
            "acceptance_token": payment_init.acceptance_token,
            "accept_personal_auth": payment_init.accept_personal_auth,
        }

    transaction = Transaction(
        integration_id=integration.id,
        provider_slug=provider_slug,
        reference=reference,
        amount_cop=amount_cop,
        currency=currency,
        status="pending",
        customer_email=customer.get("email"),
        extra_metadata=metadata,
    )
    session.add(transaction)
    await session.flush()

    checkout_out = {
        "public_key": checkout.public_key,
        "amount_in_cents": checkout.amount_in_cents,
        "currency": checkout.currency,
        "reference": checkout.reference,
        "integrity_signature": checkout.integrity_signature,
        "redirect_url": checkout.redirect_url,
        "customer_data": checkout.customer_data,
    }

    return transaction, checkout_out, payment_init_out


async def charge_payment_intent(
    session: AsyncSession,
    *,
    integration: Integration,
    reference: str,
    payment_method: PaymentMethodInput,
    provider_slug: str = "wompi",
) -> tuple[Transaction, ChargeResult]:
    """API directa: le pide al proveedor que intente cobrar una tarjeta ya
    tokenizada por el frontend de la app. El estado LOCAL de la transaccion
    se queda en `pending` pase lo que pase aca (incluso si el proveedor ya
    respondio "APPROVED" de forma sincrona) -- la fuente de verdad sigue
    siendo el webhook, ver el modulo docstring y `handle_provider_webhook`.

    Excepcion: si el proveedor ni siquiera acepta el intento
    (`ProviderRequestError`, ver `providers/base.py`), no va a haber webhook
    para esta transaccion nunca -- se marca `error` de una vez aca para no
    dejarla en `pending` indefinidamente.
    """
    transaction = await repository.get_transaction_by_reference(session, integration.id, reference)
    if transaction is None or transaction.status != "pending":
        raise TransactionNotChargeable(reference)

    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)

    try:
        result = await provider.charge(
            reference=transaction.reference,
            amount_cop=transaction.amount_cop,
            currency=transaction.currency,
            customer_email=transaction.customer_email or "",
            credentials=_credentials_data(credential),
            payment_method=payment_method,
        )
    except ProviderRequestError:
        transaction.status = "error"
        await session.commit()
        raise

    transaction.provider_transaction_id = result.provider_transaction_id
    await session.commit()

    return transaction, result


async def get_available_payment_methods(
    session: AsyncSession, *, integration: Integration, provider_slug: str = "wompi"
) -> list[str]:
    """Metodos de pago que el proveedor tiene habilitados para esta
    integracion (ya filtrados a lo que este Core sabe orquestar -- ver
    `_SUPPORTED_PAYMENT_METHODS` en `providers/wompi.py`). Pensado para que
    el consumidor arme su selector de metodo de pago ANTES de crear un
    intent, no en cada cobro."""
    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    return await provider.list_payment_methods(credentials=_credentials_data(credential))


async def get_pse_financial_institutions(
    session: AsyncSession, *, integration: Integration, provider_slug: str = "wompi"
) -> list[FinancialInstitution]:
    """Bancos disponibles para pagar por PSE, proxeados desde el proveedor."""
    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    return await provider.list_pse_financial_institutions(credentials=_credentials_data(credential))


async def create_payment_source(
    session: AsyncSession,
    *,
    integration: Integration,
    source_type: Literal["CARD", "NEQUI"],
    token: str,
    customer_email: str,
    provider_slug: str = "wompi",
) -> PaymentSource:
    """Tokeniza una tarjeta o cuenta Nequi PARA REUSO (a diferencia de
    `charge_payment_intent`, que cobra una sola vez) -- ver
    `providers/base.py` y `WompiProvider.create_payment_source`. El Core NO
    persiste el `payment_source_id` resultante: lo crea y lo devuelve: quien
    decide guardarlo contra un negocio/cliente es el consumidor (el Core no
    conoce esa semantica de negocio, igual que no conoce que es un
    "Business")."""
    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    return await provider.create_payment_source(
        credentials=_credentials_data(credential),
        source_type=source_type,
        token=token,
        customer_email=customer_email,
    )


async def void_payment_source(
    session: AsyncSession,
    *,
    integration: Integration,
    payment_source_id: str,
    provider_slug: str = "wompi",
) -> PaymentSource:
    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    return await provider.void_payment_source(
        credentials=_credentials_data(credential), payment_source_id=payment_source_id
    )


async def handle_provider_webhook(
    session: AsyncSession, *, integration: Integration, provider_slug: str, payload: dict[str, Any]
) -> Transaction | None:
    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        return None

    provider = get_provider(provider_slug)
    if not provider.verify_webhook_signature(payload, _credentials_data(credential)):
        raise PermissionError("Firma de webhook invalida.")

    event = provider.parse_webhook_event(payload)
    if event is None:
        return None

    transaction = await repository.get_transaction_by_reference(session, integration.id, event.reference)
    if transaction is None or transaction.status != "pending":
        # Ya procesada (reintento del proveedor) o no la iniciamos nosotros:
        # idempotente, no reprocesar ni renotificar.
        return transaction

    transaction.provider_transaction_id = event.provider_transaction_id
    transaction.payload = event.raw
    transaction.status = event.kind

    if event.kind == "approved":
        fee_schedule = await repository.get_active_fee_schedule(session, integration.id, provider_slug)
        if fee_schedule is not None:
            fee = calculate_fee_cop(
                transaction.amount_cop,
                percent_fee=fee_schedule.percent_fee,
                fixed_fee_cop=fee_schedule.fixed_fee_cop,
                iva_percent=fee_schedule.iva_percent,
            )
            transaction.fee_cop = fee
            transaction.net_amount_cop = transaction.amount_cop - fee
        transaction.confirmed_at = datetime.utcnow()

    await session.flush()

    webhook_event = _EVENT_BY_KIND.get(event.kind, "payment.pending")
    await dispatch_transaction_event(session, transaction=transaction, integration=integration, event=webhook_event)
    await session.commit()

    return transaction
