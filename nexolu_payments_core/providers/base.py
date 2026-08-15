"""Contrato que debe cumplir cualquier proveedor de pagos (Wompi hoy, otro
manana). Solo Wompi esta implementado (ver `wompi.py`) pero el resto del
Core (servicio, endpoints, dispatcher de webhooks) programa contra esta
interfaz, no contra Wompi directamente -- agregar un proveedor nuevo es
escribir una clase que la cumpla y darla de alta en `registry.py`, sin tocar
`core/`.

Dos familias de capacidades conviven aca a proposito (ver docs/
APP_INTEGRATION.md, seccion "Direct API"):

- Checkout por Widget (legado, sigue funcionando): `build_checkout` arma
  parametros 100% locales (ninguna llamada de red) para que el frontend de
  la app abra el widget hospedado por el proveedor.
- Checkout por API directa (nuevo): `build_payment_init` y `charge` SI
  hacen llamadas de red al proveedor -- son `async` porque el Core mismo
  orquesta la creacion de la transaccion contra la API del proveedor, en
  vez de delegarsela a un widget en el navegador del usuario.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCredentialsData:
    """Vista de solo lectura de un `ProviderCredential` ya descifrado, para
    que el proveedor no dependa del modelo de persistencia."""

    public_key: str
    private_key: str
    integrity_secret: str
    events_secret: str


@dataclass(frozen=True)
class CheckoutParams:
    """Lo que el proveedor necesita para que el frontend de la app
    integradora abra su widget/redireccion de pago (flujo legado)."""

    public_key: str
    amount_in_cents: int
    currency: str
    reference: str
    integrity_signature: str
    redirect_url: str
    customer_data: dict[str, Any]


@dataclass(frozen=True)
class PaymentInitData:
    """Lo que el frontend de la app integradora necesita para completar un
    cobro por API directa SIN abrir el widget del proveedor:

    - `public_key`: para que el frontend tokenice la tarjeta hablando
      directamente con el proveedor (nunca con el Core -- el numero de
      tarjeta no debe tocar nuestros servidores, ver regla de "no manejar
      datos sensibles de tarjeta innecesariamente").
    - `acceptance_token` / `accept_personal_auth`: tokens de aceptacion
      legal (politica de tratamiento de datos / terminos) que el proveedor
      exige mostrarle al usuario y luego reenviar al cobrar. Se obtienen en
      vivo del proveedor (no son locales, por eso `build_payment_init` es
      `async`), a diferencia de `integrity_signature`, que si es un calculo
      local determinista (misma formula que el checkout de Widget).
    """

    public_key: str
    amount_in_cents: int
    currency: str
    reference: str
    integrity_signature: str
    acceptance_token: str
    accept_personal_auth: str


@dataclass(frozen=True)
class CardPaymentMethod:
    """Metodo de pago con tarjeta ya tokenizada. El token lo genero el
    frontend de la app integradora hablando directamente con el proveedor
    (usando el `public_key` de `PaymentInitData`) -- el Core recibe el
    token, nunca el numero de tarjeta."""

    token: str
    installments: int = 1


@dataclass(frozen=True)
class ChargeResult:
    """Resultado INMEDIATO (sincrono) de pedirle al proveedor que intente
    cobrar. Deliberadamente NO es la fuente de verdad del estado final de la
    transaccion -- eso lo sigue siendo el webhook del proveedor
    (`ProviderEvent`, ver `handle_provider_webhook`). Sirve para que el Core
    tenga de una vez el id de transaccion del proveedor y la app pueda
    mostrarle algo al usuario mientras espera la confirmacion async."""

    provider_transaction_id: str
    raw_status: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProviderEvent:
    """Evento de webhook del proveedor ya normalizado. `kind` es agnostico
    del proveedor (approved|declined|error|voided|pending); `raw_status` es
    el valor tal cual lo mando el proveedor, por si hace falta para debug."""

    kind: str
    reference: str
    provider_transaction_id: str | None
    raw_status: str
    raw: dict[str, Any]


class ProviderRequestError(Exception):
    """Fallo al hablar con el proveedor para INICIAR o CONFIRMAR un cobro
    (red, timeout, 4xx/5xx, respuesta con forma inesperada).

    Distinta de una transaccion "declinada": esto significa que el
    proveedor nunca llego a aceptar el intento, asi que nunca va a mandar un
    webhook para el -- el Core la captura para marcar la transaccion como
    `error` de inmediato en vez de dejarla en `pending` para siempre
    esperando un webhook que no va a llegar (ver
    `service.charge_payment_intent`)."""


class PaymentProvider(Protocol):
    slug: str

    def build_checkout(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        credentials: ProviderCredentialsData,
        redirect_url: str,
        customer: dict[str, Any],
    ) -> CheckoutParams: ...

    async def build_payment_init(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        credentials: ProviderCredentialsData,
    ) -> PaymentInitData: ...

    async def charge(
        self,
        *,
        reference: str,
        amount_cop: int,
        currency: str,
        customer_email: str,
        credentials: ProviderCredentialsData,
        payment_method: CardPaymentMethod,
    ) -> ChargeResult: ...

    def verify_webhook_signature(self, payload: dict[str, Any], credentials: ProviderCredentialsData) -> bool: ...

    def parse_webhook_event(self, payload: dict[str, Any]) -> ProviderEvent | None: ...
