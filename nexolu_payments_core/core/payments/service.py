"""Orquestador de pagos: une proveedor + persistencia + comision + webhook
saliente. Los endpoints HTTP (`api/v1/payments.py`, `api/v1/webhooks.py`) son
delgados a proposito -- toda la logica vive aca para poder probarla sin
levantar la app.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.entities import Integration, ProviderCredential, Transaction
from nexolu_payments_core.core.payments.fees import calculate_fee_cop
from nexolu_payments_core.core.webhooks.dispatcher import dispatch_transaction_event
from nexolu_payments_core.providers.base import ProviderCredentialsData
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
) -> tuple[Transaction, dict[str, Any]]:
    if await repository.get_transaction_by_reference(session, integration.id, reference):
        raise DuplicateReference(reference)

    credential = await repository.get_active_credential(session, integration.id, provider_slug)
    if credential is None:
        raise IntegrationNotConfigured(
            f"La integracion '{integration.slug}' no tiene credenciales activas de '{provider_slug}'."
        )

    provider = get_provider(provider_slug)
    checkout = provider.build_checkout(
        reference=reference,
        amount_cop=amount_cop,
        currency=currency,
        credentials=_credentials_data(credential),
        redirect_url=redirect_url,
        customer=customer,
    )

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

    return transaction, checkout_out


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
