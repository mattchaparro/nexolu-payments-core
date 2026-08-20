"""Helpers for non-reversible Integration API key storage."""
from __future__ import annotations

import hashlib
import uuid


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_secret(prefix: str) -> str:
    """Genera un secreto random con un prefijo legible (nxl_..., whsec_...) -
    usado tanto al crear una Integration (entities.py) como al regenerar sus
    secretos (api/v1/payments.py)."""
    return f"{prefix}_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
