"""Registro de proveedores de pago disponibles.

Agregar un proveedor nuevo (PayU, Stripe...): escribir una clase que cumpla
`PaymentProvider` (ver `base.py`) en un modulo nuevo de este paquete y
agregarla aca. Nada mas del Core cambia.
"""
from __future__ import annotations

from nexolu_payments_core.providers.base import PaymentProvider
from nexolu_payments_core.providers.wompi import WompiProvider

_PROVIDERS: dict[str, PaymentProvider] = {
    "wompi": WompiProvider(),
}


def get_provider(slug: str) -> PaymentProvider:
    provider = _PROVIDERS.get(slug)
    if provider is None:
        raise KeyError(f"Proveedor de pagos desconocido: {slug}")
    return provider
