"""Fixtures compartidas.

`Settings`, el engine de BD y el Fernet de `core/security/crypto.py` estan
cacheados con `lru_cache` (a proposito: son singletons de proceso en
produccion). Para que cada test corra aislado con su propia base de datos,
este fixture limpia esos caches antes y despues de cada test.
"""
from __future__ import annotations

import pytest

TEST_MASTER_KEY = "wLQAPfdYOhoEWkiIv14sWmQEg-8O8Fknr6OFW-9Nrw4="


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAYMENTS_MASTER_KEY", TEST_MASTER_KEY)

    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    from nexolu_payments_core.config import get_settings
    from nexolu_payments_core.core.memory.db import get_engine, get_sessionmaker
    from nexolu_payments_core.core.security.crypto import _fernet

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    _fernet.cache_clear()
