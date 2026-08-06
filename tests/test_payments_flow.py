"""Pruebas de punta a punta contra la app en memoria (ASGI transport, sin
bind de socket real): crear un intent de pago, simular el webhook de Wompi
confirmandolo, y verificar que el Core notifica a la app integradora."""
from __future__ import annotations

import hashlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from nexolu_payments_core.core.memory.db import get_engine, get_sessionmaker, init_models
from nexolu_payments_core.core.memory.entities import FeeSchedule, Integration, ProviderCredential
from nexolu_payments_core.core.webhooks.signing import verify_signature

API_KEY = "dev-pos-key"
WEBHOOK_SECRET = "dev-webhook-secret"
WOMPI_EVENTS_SECRET = "dev-events-secret"
WOMPI_INTEGRITY_SECRET = "dev-integrity-secret"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


async def _seed_integration(*, webhook_url: str = "http://pos.test/webhook") -> str:
    async with get_sessionmaker()() as session:
        integration = Integration(
            slug="pos-legacy",
            name="Nexolu POS",
            api_key=API_KEY,
            webhook_url=webhook_url,
            webhook_secret=WEBHOOK_SECRET,
        )
        session.add(integration)
        await session.flush()

        session.add(
            ProviderCredential(
                integration_id=integration.id,
                provider_slug="wompi",
                public_key="pub_test_123",
                private_key="prv_test_123",
                integrity_secret=WOMPI_INTEGRITY_SECRET,
                events_secret=WOMPI_EVENTS_SECRET,
            )
        )
        session.add(FeeSchedule(integration_id=integration.id, provider_slug="wompi"))
        await session.commit()
        return integration.id


def _wompi_webhook_payload(*, reference: str, status: str, provider_transaction_id: str = "tx_1") -> dict:
    timestamp = "1700000000"
    parts = [provider_transaction_id, status, timestamp, WOMPI_EVENTS_SECRET]
    checksum = hashlib.sha256("".join(parts).encode()).hexdigest()
    return {
        "event": "transaction.updated",
        "timestamp": timestamp,
        "data": {
            "transaction": {
                "id": provider_transaction_id,
                "status": status,
                "reference": reference,
                "amount_in_cents": 5_000_000,
                "payment_method_type": "CARD",
            }
        },
        "signature": {"properties": ["transaction.id", "transaction.status"], "checksum": checksum},
    }


@pytest.fixture
async def client():
    await init_models()
    from nexolu_payments_core.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await get_engine().dispose()


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_intent_requires_authorization(client):
    response = await client.post("/v1/payments/intents", json={})
    assert response.status_code == 401


async def test_create_intent_without_credentials_returns_503(client):
    async with get_sessionmaker()() as session:
        session.add(Integration(slug="pos-legacy", name="Nexolu POS", api_key=API_KEY, webhook_secret="s"))
        await session.commit()

    response = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-1",
            "amount_cop": 50_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "a@test.com"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 503


async def test_create_intent_returns_checkout_params(client):
    await _seed_integration()

    response = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-1-2026",
            "amount_cop": 50_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com", "full_name": "Cliente Test"},
        },
        headers=HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["checkout"]["amount_in_cents"] == 5_000_000
    assert body["checkout"]["public_key"] == "pub_test_123"


async def test_create_intent_duplicate_reference_conflicts(client):
    await _seed_integration()
    body = {
        "reference": "NEX-DUP",
        "amount_cop": 10_000,
        "redirect_url": "https://app.test/billing",
        "customer": {"email": "a@test.com"},
    }

    first = await client.post("/v1/payments/intents", json=body, headers=HEADERS)
    assert first.status_code == 201

    second = await client.post("/v1/payments/intents", json=body, headers=HEADERS)
    assert second.status_code == 409


async def test_wompi_webhook_unknown_integration_returns_401(client):
    response = await client.post("/v1/webhooks/wompi/no-existe", json=_wompi_webhook_payload(
        reference="NEX-1", status="APPROVED"
    ))
    assert response.status_code == 401


async def test_wompi_webhook_invalid_signature_returns_401(client):
    await _seed_integration()
    payload = _wompi_webhook_payload(reference="NEX-1", status="APPROVED")
    payload["signature"]["checksum"] = "tampered"

    response = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert response.status_code == 401


async def test_wompi_webhook_approves_transaction_and_dispatches_signed_webhook(client, httpx_mock):
    await _seed_integration()

    intent = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-1-2026",
            "amount_cop": 100_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
        },
        headers=HEADERS,
    )
    assert intent.status_code == 201

    httpx_mock.add_response(url="http://pos.test/webhook", json={"ok": True})

    payload = _wompi_webhook_payload(reference="NEX-1-2026", status="APPROVED")
    response = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert response.status_code == 200

    status_response = await client.get("/v1/payments/transactions/NEX-1-2026", headers=HEADERS)
    body = status_response.json()
    assert body["status"] == "approved"
    assert body["fee_cop"] == 3987  # 100000*2.65% + 700, +19% IVA
    assert body["net_amount_cop"] == body["amount_cop"] - body["fee_cop"]
    assert body["provider_transaction_id"] == "tx_1"

    dispatched = httpx_mock.get_requests(url="http://pos.test/webhook")[0]
    sent_body = json.loads(dispatched.content)
    assert sent_body["event"] == "payment.approved"
    assert sent_body["reference"] == "NEX-1-2026"
    assert sent_body["integration"] == "pos-legacy"

    timestamp = int(dispatched.headers["X-Nexolu-Timestamp"])
    assert verify_signature(WEBHOOK_SECRET, dispatched.content, timestamp, dispatched.headers["X-Nexolu-Signature"])


async def test_wompi_webhook_is_idempotent_on_replay(client, httpx_mock):
    await _seed_integration()
    await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-2",
            "amount_cop": 20_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "a@test.com"},
        },
        headers=HEADERS,
    )

    httpx_mock.add_response(url="http://pos.test/webhook", json={"ok": True})

    payload = _wompi_webhook_payload(reference="NEX-2", status="APPROVED")
    first = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert first.status_code == 200

    second = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert second.status_code == 200

    # Solo se despacho un webhook saliente: el segundo intento fue ignorado
    # porque la transaccion ya no estaba "pending".
    assert len(httpx_mock.get_requests(url="http://pos.test/webhook")) == 1


async def test_wompi_webhook_declined_does_not_compute_fee(client, httpx_mock):
    await _seed_integration()
    await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-3",
            "amount_cop": 20_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "a@test.com"},
        },
        headers=HEADERS,
    )

    httpx_mock.add_response(url="http://pos.test/webhook", json={"ok": True})

    payload = _wompi_webhook_payload(reference="NEX-3", status="DECLINED")
    response = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert response.status_code == 200

    status_response = await client.get("/v1/payments/transactions/NEX-3", headers=HEADERS)
    body = status_response.json()
    assert body["status"] == "declined"
    assert body["fee_cop"] is None

    dispatched = httpx_mock.get_requests(url="http://pos.test/webhook")[0]
    assert json.loads(dispatched.content)["event"] == "payment.declined"
