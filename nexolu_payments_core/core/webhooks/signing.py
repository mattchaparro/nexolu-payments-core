"""Firma de los webhooks salientes (Core -> app integradora).

Mismo espiritu que la firma que Wompi ya usa para notificar a Laravel, pero
en la direccion contraria: HMAC-SHA256 sobre `"{timestamp}.{body crudo}"`
con el `webhook_secret` de la integracion. La app integradora reconstruye la
misma cadena con el body que recibio y compara -- ver docs/APP_INTEGRATION.md
para el snippet de verificacion.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def sign_payload(secret: str, raw_body: bytes, timestamp: int | None = None) -> tuple[str, int]:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_content = f"{ts}.".encode() + raw_body
    signature = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    return signature, ts


def verify_signature(secret: str, raw_body: bytes, timestamp: int, signature: str) -> bool:
    expected, _ = sign_payload(secret, raw_body, timestamp)
    return hmac.compare_digest(expected, signature)
