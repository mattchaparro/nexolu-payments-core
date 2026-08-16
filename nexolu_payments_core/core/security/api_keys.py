"""Helpers for non-reversible Integration API key storage."""
from __future__ import annotations

import hashlib


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
