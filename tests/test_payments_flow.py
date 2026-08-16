"""Core integration tests for the multi-merchant payment lifecycle."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from nexolu_payments_core.core.memory.db import get_engine, init_models


@pytest.fixture
async def client():
    await init_models()
    from nexolu_payments_core.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value
    await get_engine().dispose()


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_payment_requires_integration_api_key(client):
    response = await client.post("/v1/payments/intents", json={})
    assert response.status_code == 401


async def test_provisioning_requires_server_key(client):
    response = await client.post("/v1/admin/merchants", json={"name": "x", "slug": "x"})
    assert response.status_code == 401


async def test_provisioning_creates_merchant(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()

    response = await client.post(
        "/v1/admin/merchants",
        headers={"X-Payments-Provisioning-Key": "provisioning-test-key"},
        json={"name": "Merchant A", "slug": "merchant-a"},
    )
    assert response.status_code == 201
    assert response.json()["slug"] == "merchant-a"


async def test_models_have_merchant_context():
    from nexolu_payments_core.core.memory.entities import Integration, ProviderCredential, Transaction

    assert "merchant_id" in Integration.__table__.columns
    assert "merchant_id" in ProviderCredential.__table__.columns
    assert "merchant_id" in Transaction.__table__.columns
    assert Transaction.__table__.c.reference.unique is True
