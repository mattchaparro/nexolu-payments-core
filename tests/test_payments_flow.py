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


async def test_integration_widget_enabled_defaults_off_and_is_patchable(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant B", "slug": "merchant-b"})).json()

    created = await client.post(
        f"/v1/admin/merchants/{merchant['id']}/integrations",
        headers=headers,
        json={"name": "App B", "slug": "app-b"},
    )
    assert created.status_code == 201
    assert created.json()["widget_enabled"] is False

    patched = await client.patch(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created.json()['id']}",
        headers=headers,
        json={"widget_enabled": True},
    )
    assert patched.status_code == 200
    assert patched.json()["widget_enabled"] is True


async def test_list_merchants(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant C", "slug": "merchant-c"})
    await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant D", "slug": "merchant-d"})

    response = await client.get("/v1/admin/merchants", headers=headers)
    assert response.status_code == 200
    slugs = {m["slug"] for m in response.json()["merchants"]}
    assert {"merchant-c", "merchant-d"} <= slugs


async def test_list_and_get_integration_never_expose_api_key(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant E", "slug": "merchant-e"})).json()
    created = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App E", "slug": "app-e"},
        )
    ).json()
    assert "api_key" in created

    listed = await client.get(f"/v1/admin/merchants/{merchant['id']}/integrations", headers=headers)
    assert listed.status_code == 200
    [integration] = listed.json()["integrations"]
    assert integration["id"] == created["id"]
    assert "api_key" not in integration
    assert "webhook_secret" not in integration

    fetched = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created['id']}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert "api_key" not in fetched.json()


async def test_regenerate_integration_secret_invalidates_old_api_key(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant F", "slug": "merchant-f"})).json()
    created = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App F", "slug": "app-f"},
        )
    ).json()
    old_api_key = created["api_key"]

    regenerated = await client.post(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created['id']}/regenerate-secret",
        headers=headers,
    )
    assert regenerated.status_code == 200
    body = regenerated.json()
    assert body["api_key"] != old_api_key
    assert body["webhook_secret"] != created["webhook_secret"]

    # El api_key viejo ya no autentica - fue reemplazado, no agregado.
    old_key_response = await client.post(
        "/v1/payments/intents",
        headers={"Authorization": f"Bearer {old_api_key}"},
        json={"amount_cop": 1000, "customer": {"email": "a@b.com"}},
    )
    assert old_key_response.status_code == 401

    new_key_response = await client.get(
        "/v1/payments/payment-methods", headers={"Authorization": f"Bearer {body['api_key']}"}
    )
    assert new_key_response.status_code != 401


async def test_regenerate_integration_secret_requires_server_key(client):
    response = await client.post("/v1/admin/merchants/x/integrations/y/regenerate-secret")
    assert response.status_code == 401


async def test_get_integration_secrets_reveals_current_values_without_changing_them(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant I", "slug": "merchant-i"})).json()
    created = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App I", "slug": "app-i"},
        )
    ).json()

    revealed = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created['id']}/secrets", headers=headers
    )
    assert revealed.status_code == 200
    body = revealed.json()
    assert body == {
        "id": created["id"],
        "merchant_id": merchant["id"],
        "api_key": created["api_key"],
        "webhook_secret": created["webhook_secret"],
    }

    # A diferencia de regenerate-secret, este endpoint es de solo lectura -
    # el api_key original sigue autenticando despues de llamarlo.
    auth_response = await client.get(
        "/v1/payments/payment-methods", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert auth_response.status_code != 401


async def test_get_integration_secrets_requires_server_key(client):
    response = await client.get("/v1/admin/merchants/x/integrations/y/secrets")
    assert response.status_code == 401


async def test_get_integration_secrets_404_for_unknown_id(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant J", "slug": "merchant-j"})).json()
    response = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/integrations/no-existe/secrets", headers=headers
    )
    assert response.status_code == 404


async def test_delete_integration_deactivates_and_blocks_auth(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant G", "slug": "merchant-g"})).json()
    created = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App G", "slug": "app-g"},
        )
    ).json()

    deleted = await client.delete(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created['id']}", headers=headers
    )
    assert deleted.status_code == 204

    fetched = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/integrations/{created['id']}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["is_active"] is False

    # El api_key de una integration desactivada ya no autentica.
    auth_response = await client.get(
        "/v1/payments/payment-methods", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert auth_response.status_code == 401


async def test_delete_integration_requires_server_key(client):
    response = await client.delete("/v1/admin/merchants/x/integrations/y")
    assert response.status_code == 401


async def test_delete_integration_404_for_unknown_id(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant H", "slug": "merchant-h"})).json()
    response = await client.delete(f"/v1/admin/merchants/{merchant['id']}/integrations/no-existe", headers=headers)
    assert response.status_code == 404


async def test_configure_wompi_then_status_reports_configured_with_only_public_key(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant K", "slug": "merchant-k"})).json()

    configured = await client.post(
        f"/v1/admin/merchants/{merchant['id']}/providers/wompi",
        headers=headers,
        json={
            "environment": "sandbox",
            "public_key": "pub_test_123",
            "private_key": "prv_test_123",
            "integrity_secret": "integ_123",
            "events_secret": "evt_123",
        },
    )
    assert configured.status_code == 201

    status_response = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/providers/wompi", headers=headers, params={"environment": "sandbox"}
    )
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["configured"] is True
    assert body["public_key"] == "pub_test_123"
    assert "private_key" not in body
    assert "integrity_secret" not in body
    assert "events_secret" not in body


async def test_configure_wompi_rejects_duplicate_for_same_environment(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant L", "slug": "merchant-l"})).json()
    payload = {
        "environment": "sandbox",
        "public_key": "pub",
        "private_key": "prv",
        "integrity_secret": "integ",
        "events_secret": "evt",
    }
    first = await client.post(f"/v1/admin/merchants/{merchant['id']}/providers/wompi", headers=headers, json=payload)
    assert first.status_code == 201

    second = await client.post(f"/v1/admin/merchants/{merchant['id']}/providers/wompi", headers=headers, json=payload)
    assert second.status_code == 409


async def test_get_wompi_secrets_reveals_all_four_credentials(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant M", "slug": "merchant-m"})).json()
    await client.post(
        f"/v1/admin/merchants/{merchant['id']}/providers/wompi",
        headers=headers,
        json={
            "environment": "sandbox",
            "public_key": "pub_reveal",
            "private_key": "prv_reveal",
            "integrity_secret": "integ_reveal",
            "events_secret": "evt_reveal",
        },
    )

    revealed = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/providers/wompi/secrets",
        headers=headers,
        params={"environment": "sandbox"},
    )
    assert revealed.status_code == 200
    assert revealed.json() == {
        "merchant_id": merchant["id"],
        "provider": "wompi",
        "environment": "sandbox",
        "public_key": "pub_reveal",
        "private_key": "prv_reveal",
        "integrity_secret": "integ_reveal",
        "events_secret": "evt_reveal",
    }


async def test_get_wompi_secrets_404_when_not_configured(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant N", "slug": "merchant-n"})).json()
    response = await client.get(
        f"/v1/admin/merchants/{merchant['id']}/providers/wompi/secrets",
        headers=headers,
        params={"environment": "sandbox"},
    )
    assert response.status_code == 404


async def test_get_wompi_secrets_requires_server_key(client):
    response = await client.get("/v1/admin/merchants/x/providers/wompi/secrets")
    assert response.status_code == 401


async def _insert_transaction(*, merchant_id: str, integration_id: str, status: str, reference: str) -> str:
    """Inserta una Transaction directo via el ORM - no hay endpoint publico
    para crear una en un estado arbitrario sin pasar por Wompi de verdad
    (ver test_wompi_provider.py para ese flujo). Devuelve el id."""
    from nexolu_payments_core.core.memory.db import get_sessionmaker
    from nexolu_payments_core.core.memory.entities import Transaction

    async with get_sessionmaker()() as session:
        transaction = Transaction(
            merchant_id=merchant_id,
            integration_id=integration_id,
            provider_slug="wompi",
            reference=reference,
            provider_transaction_id="prov-123",
            amount_cop=50000,
            status=status,
        )
        session.add(transaction)
        await session.flush()
        transaction_id = transaction.id
        await session.commit()
    return transaction_id


async def test_list_transactions_includes_webhook_delivery_status(client, monkeypatch):
    """Caso real (2026-08-20): dos transacciones aprobadas por Wompi nunca
    llegaron a notificarse a pos-api porque la integration no tenia
    webhook_url configurada - la lista de transacciones tiene que mostrar
    ese estado, no solo el status de Wompi."""
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant O", "slug": "merchant-o"})).json()
    integration = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App O", "slug": "app-o"},
        )
    ).json()
    transaction_id = await _insert_transaction(
        merchant_id=merchant["id"], integration_id=integration["id"], status="approved", reference="pay_test_ref_o"
    )

    from nexolu_payments_core.core.memory.db import get_sessionmaker
    from nexolu_payments_core.core.memory.entities import WebhookDelivery

    async with get_sessionmaker()() as session:
        session.add(
            WebhookDelivery(
                transaction_id=transaction_id,
                integration_id=integration["id"],
                event="payment.approved",
                url="",
                payload={},
                last_error="La integracion no tiene webhook_url configurada.",
            )
        )
        await session.commit()

    response = await client.get("/v1/admin/transactions", headers=headers, params={"merchant_id": merchant["id"]})
    assert response.status_code == 200
    [item] = response.json()["transactions"]
    assert item["id"] == transaction_id
    assert item["merchant_slug"] == "merchant-o"
    assert item["integration_slug"] == "app-o"
    assert item["status"] == "approved"
    assert item["webhook_delivered"] is False
    assert item["webhook_last_error"] == "La integracion no tiene webhook_url configurada."


async def test_list_transactions_with_no_delivery_attempt_reports_null(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant P", "slug": "merchant-p"})).json()
    integration = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App P", "slug": "app-p"},
        )
    ).json()
    await _insert_transaction(
        merchant_id=merchant["id"], integration_id=integration["id"], status="pending", reference="pay_test_ref_p"
    )

    response = await client.get("/v1/admin/transactions", headers=headers, params={"merchant_id": merchant["id"]})
    [item] = response.json()["transactions"]
    assert item["webhook_delivered"] is None
    assert item["webhook_last_error"] is None


async def test_list_transactions_filters_by_status(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant Q", "slug": "merchant-q"})).json()
    integration = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App Q", "slug": "app-q"},
        )
    ).json()
    await _insert_transaction(merchant_id=merchant["id"], integration_id=integration["id"], status="approved", reference="pay_q_1")
    await _insert_transaction(merchant_id=merchant["id"], integration_id=integration["id"], status="declined", reference="pay_q_2")

    response = await client.get(
        "/v1/admin/transactions", headers=headers, params={"merchant_id": merchant["id"], "status": "declined"}
    )
    transactions = response.json()["transactions"]
    assert len(transactions) == 1
    assert transactions[0]["status"] == "declined"


async def test_list_transactions_requires_server_key(client):
    response = await client.get("/v1/admin/transactions")
    assert response.status_code == 401


async def test_redeliver_webhook_retries_and_succeeds(client, monkeypatch, httpx_mock):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant R", "slug": "merchant-r"})).json()
    integration = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App R", "slug": "app-r", "webhook_url": "https://integrator.example.com/webhooks/payments-core"},
        )
    ).json()
    transaction_id = await _insert_transaction(
        merchant_id=merchant["id"], integration_id=integration["id"], status="approved", reference="pay_test_ref_r"
    )
    httpx_mock.add_response(url="https://integrator.example.com/webhooks/payments-core", json={"ok": True})

    response = await client.post(f"/v1/admin/transactions/{transaction_id}/redeliver-webhook", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["event"] == "payment.approved"
    assert body["delivered"] is True
    assert body["last_status_code"] == 200


async def test_redeliver_webhook_reports_failure_without_webhook_url(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    merchant = (await client.post("/v1/admin/merchants", headers=headers, json={"name": "Merchant S", "slug": "merchant-s"})).json()
    integration = (
        await client.post(
            f"/v1/admin/merchants/{merchant['id']}/integrations",
            headers=headers,
            json={"name": "App S", "slug": "app-s"},
        )
    ).json()
    transaction_id = await _insert_transaction(
        merchant_id=merchant["id"], integration_id=integration["id"], status="approved", reference="pay_test_ref_s"
    )

    response = await client.post(f"/v1/admin/transactions/{transaction_id}/redeliver-webhook", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is False
    assert body["last_error"] == "La integracion no tiene webhook_url configurada."


async def test_redeliver_webhook_404_for_unknown_transaction(client, monkeypatch):
    monkeypatch.setenv("PROVISIONING_KEY", "provisioning-test-key")
    from nexolu_payments_core.config import get_settings
    get_settings.cache_clear()
    headers = {"X-Payments-Provisioning-Key": "provisioning-test-key"}

    response = await client.post("/v1/admin/transactions/no-existe/redeliver-webhook", headers=headers)
    assert response.status_code == 404


async def test_redeliver_webhook_requires_server_key(client):
    response = await client.post("/v1/admin/transactions/x/redeliver-webhook")
    assert response.status_code == 401


async def test_models_have_merchant_context():
    from nexolu_payments_core.core.memory.entities import Integration, ProviderCredential, Transaction

    assert "merchant_id" in Integration.__table__.columns
    assert "merchant_id" in ProviderCredential.__table__.columns
    assert "merchant_id" in Transaction.__table__.columns
    assert Transaction.__table__.c.reference.unique is True
