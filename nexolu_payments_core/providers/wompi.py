"""Proveedor Wompi.

Logica portada 1:1 de `pos-saas-legacy` (`app/Services/WompiService.php`,
`app/Http/Controllers/WompiWebhookController.php`), parametrizada por
credenciales en vez de leerlas de `config/billing.php`:

- `build_checkout`: mismo checkout embebido client-side de siempre (el
  Core nunca toca datos de tarjeta). Genera la firma de integridad
  (`sha256(reference + amount_in_cents + currency + integrity_secret)`) que
  el frontend de la app integradora pasa al widget de Wompi.
- `verify_webhook_signature`: mismo checksum que `WompiService::
  verifyWebhookSignature` -- sha256 de los valores en `signature.properties`
  (rutas dentro de `data`, en orden) + `timestamp` + `events_secret`.
- `parse_webhook_event`: mismo evento que procesa `WompiWebhookController`
  (`transaction.updated`), normalizado a los `kind` agnosticos del Core.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from nexolu_payments_core.providers.base import (
    CheckoutParams,
    ProviderCredentialsData,
    ProviderEvent,
)

_STATUS_TO_KIND = {
    "APPROVED": "approved",
    "DECLINED": "declined",
    "ERROR": "error",
    "VOIDED": "voided",
    "PENDING": "pending",
}


class WompiProvider:
    slug = "wompi"

    def build_checkout(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        credentials: ProviderCredentialsData,
        redirect_url: str,
        customer: dict[str, Any],
    ) -> CheckoutParams:
        amount_in_cents = amount_cop * 100
        signature = self._integrity_signature(reference, amount_in_cents, currency, credentials.integrity_secret)

        return CheckoutParams(
            public_key=credentials.public_key,
            amount_in_cents=amount_in_cents,
            currency=currency,
            reference=reference,
            integrity_signature=signature,
            redirect_url=redirect_url,
            customer_data=customer,
        )

    @staticmethod
    def _integrity_signature(reference: str, amount_in_cents: int, currency: str, secret: str) -> str:
        raw = f"{reference}{amount_in_cents}{currency}{secret}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def verify_webhook_signature(self, payload: dict[str, Any], credentials: ProviderCredentialsData) -> bool:
        secret = credentials.events_secret
        if not secret:
            return False

        signature = payload.get("signature") or {}
        properties = signature.get("properties") or []
        checksum = signature.get("checksum") or ""
        timestamp = str(payload.get("timestamp") or "")

        # Las propiedades son rutas relativas a payload["data"] (ej:
        # "transaction.id"), igual que del lado de Laravel.
        data = payload.get("data") or {}
        parts: list[str] = []
        for dot_path in properties:
            value: Any = data
            for key in str(dot_path).split("."):
                value = value.get(key, "") if isinstance(value, dict) else ""
            parts.append(str(value))
        parts.append(timestamp)
        parts.append(secret)

        expected = hashlib.sha256("".join(parts).encode()).hexdigest()
        return hmac.compare_digest(expected, checksum)

    def parse_webhook_event(self, payload: dict[str, Any]) -> ProviderEvent | None:
        if payload.get("event") != "transaction.updated":
            return None

        transaction = (payload.get("data") or {}).get("transaction") or {}
        reference = transaction.get("reference") or ""
        if not reference:
            return None

        raw_status = transaction.get("status") or ""
        kind = _STATUS_TO_KIND.get(raw_status, "pending")

        return ProviderEvent(
            kind=kind,
            reference=reference,
            provider_transaction_id=transaction.get("id"),
            raw_status=raw_status,
            raw=transaction,
        )
