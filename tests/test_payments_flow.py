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

# La integracion de prueba usa credenciales con prefijo `_test_`
# (pub_test_123/prv_test_123, ver _seed_integration): WompiProvider elige el
# base URL de sandbox a partir de ahi.
WOMPI_MERCHANT_URL = "https://sandbox.wompi.co/v1/merchants/pub_test_123"
WOMPI_TRANSACTIONS_URL = "https://sandbox.wompi.co/v1/transactions"


def _mock_wompi_merchant(httpx_mock, *, acceptance_token: str = "accept_xyz") -> None:
    httpx_mock.add_response(
        url=WOMPI_MERCHANT_URL,
        json={
            "data": {
                "presigned_acceptance": {"acceptance_token": acceptance_token, "type": "END_USER_POLICY"},
                "presigned_personal_data_auth": {"acceptance_token": "personal_xyz", "type": "PERSONAL_DATA_AUTH"},
            }
        },
    )


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


# ---------------------------------------------------------------------
# API directa (flow="api" + POST /intents/{reference}/charge): mismo
# webhook de siempre sigue siendo la fuente de verdad, solo cambia como se
# le pide a Wompi que intente el cobro.
# ---------------------------------------------------------------------


async def test_create_intent_flow_api_returns_payment_init_without_touching_checkout(client, httpx_mock):
    await _seed_integration()
    _mock_wompi_merchant(httpx_mock)

    response = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-API-1",
            "amount_cop": 50_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
            "flow": "api",
        },
        headers=HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    # El flujo Widget legado se sigue calculando igual (coexisten).
    assert body["checkout"]["amount_in_cents"] == 5_000_000
    assert body["payment_init"]["public_key"] == "pub_test_123"
    assert body["payment_init"]["acceptance_token"] == "accept_xyz"
    assert body["payment_init"]["accept_personal_auth"] == "personal_xyz"
    assert body["payment_init"]["amount_in_cents"] == 5_000_000


async def test_create_intent_flow_widget_default_omits_payment_init(client):
    # Sin flow (o flow="widget"): CERO llamadas a Wompi para acceptance
    # tokens y la respuesta no trae `payment_init` -- compatibilidad total
    # con integraciones existentes que no conocen el flujo nuevo.
    await _seed_integration()

    response = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-WIDGET-1",
            "amount_cop": 50_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
        },
        headers=HEADERS,
    )

    assert response.status_code == 201
    assert "payment_init" not in response.json()


async def test_charge_intent_creates_wompi_transaction_and_keeps_status_pending(client, httpx_mock):
    await _seed_integration()
    _mock_wompi_merchant(httpx_mock)

    intent = await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-API-2",
            "amount_cop": 80_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
            "flow": "api",
        },
        headers=HEADERS,
    )
    assert intent.status_code == 201

    _mock_wompi_merchant(httpx_mock)
    httpx_mock.add_response(
        url=WOMPI_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": "wompi-tx-42", "status": "PENDING", "reference": "NEX-API-2"}},
    )

    charge = await client.post(
        "/v1/payments/intents/NEX-API-2/charge",
        json={"payment_method": {"type": "CARD", "token": "tok_test_999", "installments": 1}},
        headers=HEADERS,
    )

    assert charge.status_code == 200
    body = charge.json()
    assert body["status"] == "pending"  # sigue pending: el webhook manda, no la respuesta sincrona.
    assert body["provider_transaction_id"] == "wompi-tx-42"
    assert body["provider_status"] == "PENDING"

    status_response = await client.get("/v1/payments/transactions/NEX-API-2", headers=HEADERS)
    assert status_response.json()["status"] == "pending"


async def test_charge_intent_then_webhook_approves_exactly_like_widget_flow(client, httpx_mock):
    await _seed_integration()
    _mock_wompi_merchant(httpx_mock)

    await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-API-3",
            "amount_cop": 100_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
            "flow": "api",
        },
        headers=HEADERS,
    )

    _mock_wompi_merchant(httpx_mock)
    httpx_mock.add_response(
        url=WOMPI_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": "tx_1", "status": "PENDING", "reference": "NEX-API-3"}},
    )
    charge = await client.post(
        "/v1/payments/intents/NEX-API-3/charge",
        json={"payment_method": {"type": "CARD", "token": "tok_test_999"}},
        headers=HEADERS,
    )
    assert charge.status_code == 200

    httpx_mock.add_response(url="http://pos.test/webhook", json={"ok": True})
    payload = _wompi_webhook_payload(reference="NEX-API-3", status="APPROVED", provider_transaction_id="tx_1")
    webhook_response = await client.post("/v1/webhooks/wompi/pos-legacy", json=payload)
    assert webhook_response.status_code == 200

    status_response = await client.get("/v1/payments/transactions/NEX-API-3", headers=HEADERS)
    body = status_response.json()
    assert body["status"] == "approved"
    assert body["fee_cop"] == 3987
    assert body["provider_transaction_id"] == "tx_1"


async def test_charge_intent_without_pending_transaction_returns_404(client):
    await _seed_integration()

    response = await client.post(
        "/v1/payments/intents/NEX-DOES-NOT-EXIST/charge",
        json={"payment_method": {"type": "CARD", "token": "tok_test_999"}},
        headers=HEADERS,
    )
    assert response.status_code == 404


async def test_charge_intent_provider_error_marks_transaction_error_without_breaking_core(client, httpx_mock):
    # Simula que Wompi nunca acepta el intento de cobro (network/4xx antes de
    # crear la transaccion en Wompi): no va a haber webhook para este
    # reference, asi que el Core debe marcarla error de una vez -- y el
    # endpoint debe responder con un error controlado (502), no un 500.
    await _seed_integration()
    _mock_wompi_merchant(httpx_mock)

    await client.post(
        "/v1/payments/intents",
        json={
            "reference": "NEX-API-ERR",
            "amount_cop": 30_000,
            "redirect_url": "https://app.test/billing",
            "customer": {"email": "cliente@test.com"},
            "flow": "api",
        },
        headers=HEADERS,
    )

    _mock_wompi_merchant(httpx_mock)
    httpx_mock.add_response(
        url=WOMPI_TRANSACTIONS_URL,
        method="POST",
        status_code=422,
        json={"error": {"type": "INPUT_VALIDATION_ERROR", "reason": "token_invalido", "messages": {}}},
    )

    response = await client.post(
        "/v1/payments/intents/NEX-API-ERR/charge",
        json={"payment_method": {"type": "CARD", "token": "tok_invalido"}},
        headers=HEADERS,
    )

    assert response.status_code == 502

    status_response = await client.get("/v1/payments/transactions/NEX-API-ERR", headers=HEADERS)
    assert status_response.json()["status"] == "error"
