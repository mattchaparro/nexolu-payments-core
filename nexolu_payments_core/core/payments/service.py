"""Payment orchestration independent of any HTTP framework or provider."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.entities import Integration, ProviderCredential, Transaction
from nexolu_payments_core.core.payments.fees import calculate_fee_cop
from nexolu_payments_core.core.webhooks.dispatcher import dispatch_transaction_event
from nexolu_payments_core.providers.base import ChargeResult, FinancialInstitution, PaymentMethodInput, PaymentSource, ProviderCredentialsData, ProviderRequestError
from nexolu_payments_core.providers.registry import get_provider

_EVENT_BY_KIND = {"approved": "payment.approved", "declined": "payment.declined", "error": "payment.error", "voided": "payment.voided"}


class IntegrationNotConfigured(Exception):
    pass


class DuplicateReference(Exception):
    pass


class TransactionNotChargeable(Exception):
    pass


def generate_reference() -> str:
    """Generate the Core-owned payment reference sent to the provider."""
    return f"pay_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"


def _credentials_data(credential: ProviderCredential) -> ProviderCredentialsData:
    return ProviderCredentialsData(
        public_key=credential.public_key,
        private_key=credential.private_key,
        integrity_secret=credential.integrity_secret,
        events_secret=credential.events_secret,
    )


async def _credential(session: AsyncSession, integration: Integration, provider_slug: str) -> ProviderCredential:
    credential = await repository.get_active_credential(session, integration.merchant_id, provider_slug, integration.environment)
    if credential is None:
        raise IntegrationNotConfigured(
            f"El merchant '{integration.merchant_id}' no tiene credenciales activas de '{provider_slug}' para '{integration.environment}'."
        )
    return credential


async def create_payment_intent(
    session: AsyncSession,
    *,
    integration: Integration,
    amount_cop: int,
    currency: str,
    redirect_url: str,
    customer: dict[str, Any],
    metadata: dict[str, Any],
    provider_slug: str = "wompi",
    flow: str = "widget",
) -> tuple[Transaction, dict[str, Any], dict[str, Any] | None]:
    credential = await _credential(session, integration, provider_slug)
    provider = get_provider(provider_slug)
    credentials = _credentials_data(credential)
    reference = generate_reference()

    checkout = provider.build_checkout(
        reference=reference,
        amount_cop=amount_cop,
        currency=currency,
        credentials=credentials,
        redirect_url=redirect_url,
        customer=customer,
    )

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
        merchant_id=integration.merchant_id,
        integration_id=integration.id,
        provider_slug=provider_slug,
        reference=reference,
        amount_cop=amount_cop,
        currency=currency,
        status="pending",
        customer_email=customer.get("email"),
        extra_metadata=metadata,
        redirect_url=redirect_url,
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
    transaction = await repository.get_transaction_by_reference(session, reference)
    if transaction is None or transaction.integration_id != integration.id or transaction.status != "pending":
        raise TransactionNotChargeable(reference)

    credential = await _credential(session, integration, provider_slug)
    provider = get_provider(provider_slug)
    try:
        result = await provider.charge(
            reference=transaction.reference,
            amount_cop=transaction.amount_cop,
            currency=transaction.currency,
            customer_email=transaction.customer_email or "",
            credentials=_credentials_data(credential),
            payment_method=payment_method,
            redirect_url=transaction.redirect_url,
        )
    except ProviderRequestError:
        transaction.status = "error"
        await session.commit()
        raise

    transaction.provider_transaction_id = result.provider_transaction_id
    await session.commit()
    return transaction, result


async def get_available_payment_methods(session: AsyncSession, *, integration: Integration, provider_slug: str = "wompi") -> list[str]:
    credential = await _credential(session, integration, provider_slug)
    return await get_provider(provider_slug).list_payment_methods(credentials=_credentials_data(credential))


async def get_pse_financial_institutions(session: AsyncSession, *, integration: Integration, provider_slug: str = "wompi") -> list[FinancialInstitution]:
    credential = await _credential(session, integration, provider_slug)
    return await get_provider(provider_slug).list_pse_financial_institutions(credentials=_credentials_data(credential))


async def create_payment_source(session: AsyncSession, *, integration: Integration, source_type: Literal["CARD", "NEQUI"], token: str, customer_email: str, provider_slug: str = "wompi") -> PaymentSource:
    credential = await _credential(session, integration, provider_slug)
    return await get_provider(provider_slug).create_payment_source(credentials=_credentials_data(credential), source_type=source_type, token=token, customer_email=customer_email)


async def void_payment_source(session: AsyncSession, *, integration: Integration, payment_source_id: str, provider_slug: str = "wompi") -> PaymentSource:
    credential = await _credential(session, integration, provider_slug)
    return await get_provider(provider_slug).void_payment_source(credentials=_credentials_data(credential), payment_source_id=payment_source_id)


async def handle_provider_webhook(session: AsyncSession, *, provider_slug: str, payload: dict[str, Any]) -> Transaction | None:
    event = get_provider(provider_slug).parse_webhook_event(payload)
    if event is None:
        return None

    transaction = await repository.get_transaction_by_reference(session, event.reference)
    if transaction is None or transaction.provider_slug != provider_slug:
        return None

    credential = await repository.get_active_credential(session, transaction.merchant_id, provider_slug)
    if credential is None:
        return None

    provider = get_provider(provider_slug)
    if not provider.verify_webhook_signature(payload, _credentials_data(credential)):
        raise PermissionError("Firma de webhook invalida.")

    if transaction.status != "pending":
        return transaction

    transaction.provider_transaction_id = event.provider_transaction_id
    transaction.payload = event.raw
    transaction.status = event.kind

    if event.kind == "approved":
        fee_schedule = await repository.get_active_fee_schedule(session, transaction.merchant_id, provider_slug)
        if fee_schedule is not None:
            fee = calculate_fee_cop(transaction.amount_cop, percent_fee=fee_schedule.percent_fee, fixed_fee_cop=fee_schedule.fixed_fee_cop, iva_percent=fee_schedule.iva_percent)
            transaction.fee_cop = fee
            transaction.net_amount_cop = transaction.amount_cop - fee
        transaction.confirmed_at = datetime.utcnow()

    await session.flush()
    integration = await repository.get_integration_by_id(session, transaction.integration_id)
    if integration is not None:
        await dispatch_transaction_event(session, transaction=transaction, integration=integration, event=_EVENT_BY_KIND.get(event.kind, "payment.pending"))
    await session.commit()
    return transaction
