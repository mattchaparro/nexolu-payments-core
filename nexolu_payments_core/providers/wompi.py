"""Proveedor Wompi.

Dos flujos conviven aca (ver docs/APP_INTEGRATION.md):

- **Widget Checkout (legado, sigue funcionando).** Logica portada 1:1 de
  `pos-saas-legacy` (`app/Services/WompiService.php`,
  `app/Http/Controllers/WompiWebhookController.php`), parametrizada por
  credenciales en vez de leerlas de `config/billing.php`:
  - `build_checkout`: checkout embebido client-side de siempre (el Core
    nunca toca datos de tarjeta). Genera la firma de integridad
    (`sha256(reference + amount_in_cents + currency + integrity_secret)`)
    que el frontend de la app integradora pasa al widget de Wompi.

- **API directa (nueva).** El Core llama directamente a la API REST de
  Wompi (`docs.wompi.co`) en vez de delegarle el cobro a un widget:
  - `build_payment_init`: pide a Wompi los tokens de aceptacion legal
    (`GET /merchants/:public_key`) que hay que mostrarle al usuario y
    reenviar al cobrar. La firma de integridad es la misma formula de
    siempre (calculo local, sin red).
  - `charge`: crea la transaccion en Wompi (`POST /transactions`) con una
    tarjeta YA tokenizada por el frontend de la app (el token lo genera el
    frontend hablando directo con Wompi usando el `public_key` -- el Core
    nunca ve el numero de tarjeta, ver `CardPaymentMethod`).

En ambos flujos, la verificacion de firma y el parseo de eventos de webhook
son EXACTAMENTE los mismos (Wompi manda el mismo evento `transaction.
updated` sin importar si la transaccion se creo por Widget o por API):
- `verify_webhook_signature`: mismo checksum que `WompiService::
  verifyWebhookSignature` -- sha256 de los valores en `signature.properties`
  (rutas dentro de `data`, en orden) + `timestamp` + `events_secret`.
- `parse_webhook_event`: mismo evento que procesa `WompiWebhookController`
  (`transaction.updated`), normalizado a los `kind` agnosticos del Core.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from nexolu_payments_core.providers.base import (
    CardPaymentMethod,
    ChargeResult,
    CheckoutParams,
    PaymentInitData,
    ProviderCredentialsData,
    ProviderEvent,
    ProviderRequestError,
)

logger = logging.getLogger(__name__)

_STATUS_TO_KIND = {
    "APPROVED": "approved",
    "DECLINED": "declined",
    "ERROR": "error",
    "VOIDED": "voided",
    "PENDING": "pending",
}

# Wompi codifica el entorno en el prefijo de sus propias llaves
# (pub_test_/prv_test_ vs pub_prod_/prv_prod_, ver docs.wompi.co
# "Ambientes y llaves") -- el proveedor lo infiere de ahi, no hace falta que
# el Core sepa de "sandbox vs production" ni que `ProviderCredentialsData`
# cargue un campo nuevo para esto.
_SANDBOX_BASE_URL = "https://sandbox.wompi.co/v1"
_PRODUCTION_BASE_URL = "https://production.wompi.co/v1"
_REQUEST_TIMEOUT_SECONDS = 15.0


def _base_url_for(credentials: ProviderCredentialsData) -> str:
    is_sandbox = "_test_" in credentials.private_key or "_test_" in credentials.public_key
    return _SANDBOX_BASE_URL if is_sandbox else _PRODUCTION_BASE_URL


def _wompi_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"

    error = (body or {}).get("error") or {}
    reason = error.get("reason") or error.get("type")
    messages = error.get("messages")
    if reason and messages:
        return f"{reason}: {messages}"
    return reason or f"HTTP {response.status_code}"


async def _fetch_acceptance_tokens(
    client: httpx.AsyncClient, *, public_key: str
) -> tuple[str, str]:
    """`GET /merchants/:public_key` -- publico (no requiere Authorization),
    devuelve los tokens de aceptacion legal vigentes del merchant (JWT de
    corta duracion, por eso se piden en vivo cada vez en vez de cachearlos)."""
    try:
        response = await client.get(f"/merchants/{public_key}")
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ProviderRequestError(
            f"Wompi rechazo la consulta del merchant: {_wompi_error_detail(exc.response)}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderRequestError(f"No se pudo consultar el merchant en Wompi: {exc}") from exc

    data = (response.json() or {}).get("data") or {}
    acceptance_token = (data.get("presigned_acceptance") or {}).get("acceptance_token")
    accept_personal_auth = (data.get("presigned_personal_data_auth") or {}).get("acceptance_token")

    if not acceptance_token:
        raise ProviderRequestError("Wompi no devolvio un acceptance_token valido para este merchant.")

    return acceptance_token, accept_personal_auth or ""


class WompiProvider:
    slug = "wompi"

    # ------------------------------------------------------------------
    # Widget Checkout (legado)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # API directa
    # ------------------------------------------------------------------

    async def build_payment_init(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        credentials: ProviderCredentialsData,
    ) -> PaymentInitData:
        amount_in_cents = amount_cop * 100
        signature = self._integrity_signature(reference, amount_in_cents, currency, credentials.integrity_secret)

        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            acceptance_token, accept_personal_auth = await _fetch_acceptance_tokens(
                client, public_key=credentials.public_key
            )

        return PaymentInitData(
            public_key=credentials.public_key,
            amount_in_cents=amount_in_cents,
            currency=currency,
            reference=reference,
            integrity_signature=signature,
            acceptance_token=acceptance_token,
            accept_personal_auth=accept_personal_auth,
        )

    async def charge(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        customer_email: str,
        credentials: ProviderCredentialsData,
        payment_method: CardPaymentMethod,
    ) -> ChargeResult:
        amount_in_cents = amount_cop * 100
        signature = self._integrity_signature(reference, amount_in_cents, currency, credentials.integrity_secret)

        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            # Los tokens de aceptacion se piden de nuevo aca (en vez de
            # confiar en los que ya se le mando al frontend en
            # build_payment_init): son JWT de Wompi, no del Core, y no hay
            # que confiar en lo que un cliente HTTP diga que Wompi le dio.
            acceptance_token, accept_personal_auth = await _fetch_acceptance_tokens(
                client, public_key=credentials.public_key
            )

            body: dict[str, Any] = {
                "amount_in_cents": amount_in_cents,
                "currency": currency,
                "customer_email": customer_email,
                "reference": reference,
                "signature": signature,
                "acceptance_token": acceptance_token,
                "payment_method": {
                    "type": "CARD",
                    "token": payment_method.token,
                    "installments": payment_method.installments,
                },
            }
            if accept_personal_auth:
                body["accept_personal_auth"] = accept_personal_auth

            try:
                response = await client.post(
                    "/transactions",
                    json=body,
                    headers={"Authorization": f"Bearer {credentials.private_key}"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderRequestError(
                    f"Wompi rechazo la creacion de la transaccion: {_wompi_error_detail(exc.response)}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderRequestError(f"No se pudo crear la transaccion en Wompi: {exc}") from exc

        data = (response.json() or {}).get("data") or {}
        provider_transaction_id = data.get("id")
        if not provider_transaction_id:
            raise ProviderRequestError("Wompi no devolvio un id de transaccion valido.")

        return ChargeResult(
            provider_transaction_id=provider_transaction_id,
            raw_status=data.get("status", ""),
            raw=data,
        )

    # ------------------------------------------------------------------
    # Webhooks (comun a ambos flujos)
    # ------------------------------------------------------------------

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
