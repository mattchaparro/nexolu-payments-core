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

import asyncio
import hashlib
import hmac
import logging
from typing import Any, Literal

import httpx

from nexolu_payments_core.providers.base import (
    BancolombiaTransferPaymentMethod,
    CardPaymentMethod,
    ChargeResult,
    CheckoutParams,
    FinancialInstitution,
    NequiPaymentMethod,
    PaymentInitData,
    PaymentMethodInput,
    PaymentSource,
    PaymentSourceChargeMethod,
    ProviderCredentialsData,
    ProviderEvent,
    ProviderRequestError,
    PsePaymentMethod,
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

# Metodos de pago que este Core sabe orquestar hoy -- Wompi puede reportar
# muchos mas como habilitados en `accepted_payment_methods` (DAVIPLATA,
# BANCOLOMBIA_QR, BNPL, SU_PLUS, CARD_POS, BANCOLOMBIA_COLLECT...), pero
# `list_payment_methods` solo expone la interseccion con esto, para que un
# integrador nunca ofrezca un boton que `charge()` no sabria procesar.
_SUPPORTED_PAYMENT_METHODS = {"CARD", "NEQUI", "PSE", "BANCOLOMBIA_TRANSFER"}

# PSE y BANCOLOMBIA_TRANSFER no traen `extra.async_payment_url` en la
# respuesta inicial de POST /transactions -- Wompi no documenta un SLA para
# cuando aparece. Se hace un polling corto y acotado (ver
# `_poll_for_redirect_url`) para no obligar a cada integrador a
# implementarlo por su cuenta; si no aparece a tiempo, `ChargeResult.
# redirect_url` queda en `None` y el consumidor sigue esperando el webhook.
_ASYNC_REDIRECT_POLL_ATTEMPTS = 8
_ASYNC_REDIRECT_POLL_INTERVAL_SECONDS = 1.0


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


async def _fetch_merchant(client: httpx.AsyncClient, *, public_key: str) -> dict[str, Any]:
    """`GET /merchants/:public_key` -- publico (no requiere Authorization).
    Trae, en la misma respuesta: los tokens de aceptacion legal vigentes
    (JWT de corta duracion, por eso se piden en vivo cada vez en vez de
    cachearlos) Y `accepted_payment_methods` (lo que ese comercio tiene
    habilitado en su propio dashboard de Wompi) -- ver `_fetch_acceptance_
    tokens` y `WompiProvider.list_payment_methods`, ambos reusan esto."""
    try:
        response = await client.get(f"/merchants/{public_key}")
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ProviderRequestError(
            f"Wompi rechazo la consulta del merchant: {_wompi_error_detail(exc.response)}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderRequestError(f"No se pudo consultar el merchant en Wompi: {exc}") from exc

    return (response.json() or {}).get("data") or {}


def _acceptance_tokens_from_merchant(data: dict[str, Any]) -> tuple[str, str]:
    acceptance_token = (data.get("presigned_acceptance") or {}).get("acceptance_token")
    accept_personal_auth = (data.get("presigned_personal_data_auth") or {}).get("acceptance_token")

    if not acceptance_token:
        raise ProviderRequestError("Wompi no devolvio un acceptance_token valido para este merchant.")

    return acceptance_token, accept_personal_auth or ""


async def _fetch_acceptance_tokens(
    client: httpx.AsyncClient, *, public_key: str
) -> tuple[str, str]:
    data = await _fetch_merchant(client, public_key=public_key)
    return _acceptance_tokens_from_merchant(data)


def _payment_method_payload(payment_method: PaymentMethodInput) -> dict[str, Any]:
    """Arma el objeto `payment_method` que exige Wompi, segun el tipo -- ver
    docs.wompi.co/docs/colombia/metodos-de-pago (forma verificada contra
    esa referencia y contra sandbox real, no supuesta)."""
    if isinstance(payment_method, CardPaymentMethod):
        return {
            "type": "CARD",
            "token": payment_method.token,
            "installments": payment_method.installments,
        }
    if isinstance(payment_method, NequiPaymentMethod):
        return {
            "type": "NEQUI",
            "phone_number": payment_method.phone_number,
        }
    if isinstance(payment_method, PsePaymentMethod):
        return {
            "type": "PSE",
            "user_type": payment_method.user_type,
            "user_legal_id_type": payment_method.user_legal_id_type,
            "user_legal_id": payment_method.user_legal_id,
            "financial_institution_code": payment_method.financial_institution_code,
            "payment_description": payment_method.payment_description,
        }
    if isinstance(payment_method, BancolombiaTransferPaymentMethod):
        return {
            "type": "BANCOLOMBIA_TRANSFER",
            # Wompi lo exige aunque el ejemplo "Pago Simple" de su doc no lo
            # muestre (si aparece en el ejemplo de "segundo medio de pago" mas
            # abajo en la misma pagina) -- confirmado con un 422 real contra
            # sandbox sin este campo. Unico valor soportado hoy segun Wompi
            # ("Por el momento unicamente esta disponible Persona Natural").
            "user_type": "PERSON",
            "payment_description": payment_method.payment_description,
            "ecommerce_url": payment_method.ecommerce_url,
        }
    raise AssertionError(f"Tipo de metodo de pago no soportado: {payment_method!r}")


def _customer_data_for(payment_method: PaymentMethodInput) -> dict[str, Any] | None:
    """PSE es el unico metodo que exige `customer_data` (telefono/nombre del
    pagador) como llave HERMANA de `payment_method` en el body de Wompi, no
    anidada dentro -- ver docs.wompi.co/docs/colombia/metodos-de-pago."""
    if isinstance(payment_method, PsePaymentMethod):
        return {
            "phone_number": payment_method.customer_phone_number,
            "full_name": payment_method.customer_full_name,
        }
    return None


async def _poll_for_redirect_url(
    client: httpx.AsyncClient, *, transaction_id: str, credentials: ProviderCredentialsData
) -> str | None:
    for _ in range(_ASYNC_REDIRECT_POLL_ATTEMPTS):
        await asyncio.sleep(_ASYNC_REDIRECT_POLL_INTERVAL_SECONDS)
        try:
            response = await client.get(
                f"/transactions/{transaction_id}",
                headers={"Authorization": f"Bearer {credentials.private_key}"},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            continue

        data = (response.json() or {}).get("data") or {}
        redirect_url = ((data.get("payment_method") or {}).get("extra") or {}).get("async_payment_url")
        if redirect_url:
            return redirect_url

    return None


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
        payment_method: PaymentMethodInput,
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
            }
            if accept_personal_auth:
                body["accept_personal_auth"] = accept_personal_auth

            if isinstance(payment_method, PaymentSourceChargeMethod):
                # Forma distinta a los demas: payment_source_id va HERMANO
                # de payment_method (no un `type` adentro), y `installments`
                # es lo unico que payment_method necesita -- Wompi lo ignora
                # si la fuente no es tarjeta. Ver docs.wompi.co/docs/
                # colombia/fuentes-de-pago, "Paso 3: Crea una transaccion".
                body["payment_source_id"] = payment_method.payment_source_id
                body["payment_method"] = {"installments": payment_method.installments}
            else:
                body["payment_method"] = _payment_method_payload(payment_method)
                customer_data = _customer_data_for(payment_method)
                if customer_data:
                    body["customer_data"] = customer_data

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

            payment_method_data = data.get("payment_method") or {}
            redirect_url = (payment_method_data.get("extra") or {}).get("async_payment_url")
            if redirect_url is None and isinstance(
                payment_method, (PsePaymentMethod, BancolombiaTransferPaymentMethod)
            ):
                redirect_url = await _poll_for_redirect_url(
                    client, transaction_id=provider_transaction_id, credentials=credentials
                )

        return ChargeResult(
            provider_transaction_id=provider_transaction_id,
            raw_status=data.get("status", ""),
            raw=data,
            redirect_url=redirect_url,
        )

    async def list_payment_methods(self, *, credentials: ProviderCredentialsData) -> list[str]:
        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            data = await _fetch_merchant(client, public_key=credentials.public_key)

        accepted = data.get("accepted_payment_methods") or []
        # Interseccion con lo que este Core sabe orquestar -- ver
        # docstring de _SUPPORTED_PAYMENT_METHODS.
        return [method for method in accepted if method in _SUPPORTED_PAYMENT_METHODS]

    async def list_pse_financial_institutions(
        self, *, credentials: ProviderCredentialsData
    ) -> list[FinancialInstitution]:
        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            try:
                response = await client.get(
                    "/pse/financial_institutions",
                    headers={"Authorization": f"Bearer {credentials.public_key}"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderRequestError(
                    f"Wompi rechazo la consulta de bancos PSE: {_wompi_error_detail(exc.response)}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderRequestError(f"No se pudo consultar los bancos PSE en Wompi: {exc}") from exc

        institutions = (response.json() or {}).get("data") or []
        return [
            FinancialInstitution(
                code=str(item.get("financial_institution_code", "")),
                name=str(item.get("financial_institution_name", "")),
            )
            for item in institutions
        ]

    async def create_payment_source(
        self,
        *,
        credentials: ProviderCredentialsData,
        source_type: Literal["CARD", "NEQUI"],
        token: str,
        customer_email: str,
    ) -> PaymentSource:
        """`POST /payment_sources` -- tokeniza una tarjeta o cuenta Nequi
        PARA REUSO (a diferencia de `charge()`, que las cobra una sola vez).
        Requiere la llave PRIVADA -- a diferencia de la tokenizacion inicial
        (`POST /tokens/cards` o `/tokens/nequi`, con llave publica, que el
        frontend de la app hace directo contra Wompi), Wompi exige que este
        paso especifico se haga desde el backend, nunca desde el navegador
        del usuario. Por eso vive aca y no en el frontend. Ver
        docs.wompi.co/docs/colombia/fuentes-de-pago."""
        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            acceptance_token, accept_personal_auth = await _fetch_acceptance_tokens(
                client, public_key=credentials.public_key
            )

            body: dict[str, Any] = {
                "type": source_type,
                "token": token,
                "customer_email": customer_email,
                "acceptance_token": acceptance_token,
            }
            if accept_personal_auth:
                body["accept_personal_auth"] = accept_personal_auth

            try:
                response = await client.post(
                    "/payment_sources",
                    json=body,
                    headers={"Authorization": f"Bearer {credentials.private_key}"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderRequestError(
                    f"Wompi rechazo la creacion de la fuente de pago: {_wompi_error_detail(exc.response)}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderRequestError(f"No se pudo crear la fuente de pago en Wompi: {exc}") from exc

        data = (response.json() or {}).get("data") or {}
        source_id = data.get("id")
        if source_id is None:
            raise ProviderRequestError("Wompi no devolvio un id de fuente de pago valido.")

        return PaymentSource(id=str(source_id), type=data.get("type", source_type), status=data.get("status", ""))

    async def void_payment_source(
        self, *, credentials: ProviderCredentialsData, payment_source_id: str
    ) -> PaymentSource:
        async with httpx.AsyncClient(
            base_url=_base_url_for(credentials), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as client:
            try:
                response = await client.put(
                    f"/payment_sources/{payment_source_id}/void",
                    headers={"Authorization": f"Bearer {credentials.private_key}"},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderRequestError(
                    f"Wompi rechazo la cancelacion de la fuente de pago: {_wompi_error_detail(exc.response)}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderRequestError(f"No se pudo cancelar la fuente de pago en Wompi: {exc}") from exc

        data = (response.json() or {}).get("data") or {}
        return PaymentSource(
            id=str(data.get("id", payment_source_id)), type=data.get("type", ""), status=data.get("status", "")
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
