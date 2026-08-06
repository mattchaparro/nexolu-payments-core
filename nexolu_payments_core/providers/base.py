"""Contrato que debe cumplir cualquier proveedor de pagos (Wompi hoy, otro
manana). Solo Wompi esta implementado (ver `wompi.py`) pero el resto del
Core (servicio, endpoints, dispatcher de webhooks) programa contra esta
interfaz, no contra Wompi directamente -- agregar un proveedor nuevo es
escribir una clase que la cumpla y darla de alta en `registry.py`, sin tocar
`core/`.
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
    integradora abra su widget/redireccion de pago."""

    public_key: str
    amount_in_cents: int
    currency: str
    reference: str
    integrity_signature: str
    redirect_url: str
    customer_data: dict[str, Any]


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

    def verify_webhook_signature(self, payload: dict[str, Any], credentials: ProviderCredentialsData) -> bool: ...

    def parse_webhook_event(self, payload: dict[str, Any]) -> ProviderEvent | None: ...
