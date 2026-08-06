"""Notificacion saliente del Core hacia la app integradora.

Cada cambio de estado de una transaccion se le manda a `integration.
webhook_url` como un evento agnostico del proveedor (ver docs/
APP_INTEGRATION.md para el contrato completo). Se reintenta un par de veces
de forma sincrona, dentro del mismo request que proceso el webhook entrante
del proveedor -- no hay cola de trabajo todavia. Si `webhook_url` no esta
configurada, o los reintentos se agotan, el intento queda igual registrado en
`WebhookDelivery` para que se pueda diagnosticar o reenviar a mano; no se
pierde silenciosamente.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.config import get_settings
from nexolu_payments_core.core.memory.entities import Integration, Transaction, WebhookDelivery
from nexolu_payments_core.core.webhooks.signing import sign_payload

logger = logging.getLogger(__name__)

# Intentos: inmediato, +1s, +2s. Backoff corto a proposito: esto corre
# sincrono dentro del webhook entrante del proveedor, no puede colgar el
# request indefinidamente.
_RETRY_BACKOFF_SECONDS = [0, 1, 2]


def _build_payload(*, transaction: Transaction, integration: Integration, event: str) -> dict[str, Any]:
    occurred_at = transaction.confirmed_at or transaction.updated_at
    return {
        "event": event,
        "integration": integration.slug,
        "transaction_id": transaction.id,
        "reference": transaction.reference,
        "provider": transaction.provider_slug,
        "provider_transaction_id": transaction.provider_transaction_id,
        "amount_cop": transaction.amount_cop,
        "currency": transaction.currency,
        "fee_cop": transaction.fee_cop,
        "net_amount_cop": transaction.net_amount_cop,
        "status": transaction.status,
        "occurred_at": occurred_at.isoformat(),
        "metadata": transaction.extra_metadata,
    }


async def dispatch_transaction_event(
    session: AsyncSession, *, transaction: Transaction, integration: Integration, event: str
) -> WebhookDelivery:
    payload = _build_payload(transaction=transaction, integration=integration, event=event)

    delivery = WebhookDelivery(
        transaction_id=transaction.id,
        integration_id=integration.id,
        event=event,
        url=integration.webhook_url or "",
        payload=payload,
    )
    session.add(delivery)

    if not integration.webhook_url:
        delivery.last_error = "La integracion no tiene webhook_url configurada."
        await session.flush()
        return delivery

    raw_body = json.dumps(payload, default=str).encode()
    signature, timestamp = sign_payload(integration.webhook_secret, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Nexolu-Signature": signature,
        "X-Nexolu-Timestamp": str(timestamp),
    }

    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
        for attempt, wait_seconds in enumerate(_RETRY_BACKOFF_SECONDS, start=1):
            if wait_seconds:
                await asyncio.sleep(wait_seconds)

            delivery.attempt_count = attempt
            try:
                response = await client.post(integration.webhook_url, content=raw_body, headers=headers)
                delivery.last_status_code = response.status_code
                if 200 <= response.status_code < 300:
                    delivery.delivered_at = datetime.utcnow()
                    break
                delivery.last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                delivery.last_error = str(exc)
                logger.warning(
                    "webhook.dispatch_failed",
                    extra={"integration": integration.slug, "attempt": attempt, "error": str(exc)},
                )

    await session.flush()
    return delivery
