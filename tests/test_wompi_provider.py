from __future__ import annotations

import hashlib
import json

import pytest

from nexolu_payments_core.providers import wompi as wompi_module
from nexolu_payments_core.providers.base import (
    BancolombiaTransferPaymentMethod,
    CardPaymentMethod,
    NequiPaymentMethod,
    PaymentSourceChargeMethod,
    ProviderCredentialsData,
    ProviderRequestError,
    PsePaymentMethod,
)
from nexolu_payments_core.providers.wompi import WompiProvider

CREDENTIALS = ProviderCredentialsData(
    public_key="pub_test_123",
    private_key="prv_test_123",
    integrity_secret="integrity_secret",
    events_secret="events_secret",
)

_MERCHANT_URL = "https://sandbox.wompi.co/v1/merchants/pub_test_123"
_TRANSACTIONS_URL = "https://sandbox.wompi.co/v1/transactions"
_PAYMENT_SOURCES_URL = "https://sandbox.wompi.co/v1/payment_sources"


def _merchant_response(*, acceptance_token: str = "accept_xyz", personal_auth_token: str = "personal_xyz") -> dict:
    return {
        "data": {
            "presigned_acceptance": {
                "acceptance_token": acceptance_token,
                "permalink": "https://wompi.co/reglamento",
                "type": "END_USER_POLICY",
            },
            "presigned_personal_data_auth": {
                "acceptance_token": personal_auth_token,
                "permalink": "https://wompi.co/tratamiento-datos",
                "type": "PERSONAL_DATA_AUTH",
            },
        }
    }


def test_build_checkout_computes_integrity_signature():
    provider = WompiProvider()
    checkout = provider.build_checkout(
        reference="NEX-1-2026",
        amount_cop=50_000,
        currency="COP",
        credentials=CREDENTIALS,
        redirect_url="https://app.test/billing",
        customer={"email": "cliente@test.com"},
    )

    assert checkout.amount_in_cents == 5_000_000
    expected = hashlib.sha256(b"NEX-1-2026" + b"5000000" + b"COP" + b"integrity_secret").hexdigest()
    assert checkout.integrity_signature == expected


def _signed_payload(*, reference: str, status: str, secret: str = "events_secret") -> dict:
    timestamp = "1700000000"
    properties = ["transaction.id", "transaction.status"]
    data = {"transaction": {"id": "tx_1", "status": status, "reference": reference}}
    parts = ["tx_1", status, timestamp, secret]
    checksum = hashlib.sha256("".join(parts).encode()).hexdigest()
    return {
        "event": "transaction.updated",
        "timestamp": timestamp,
        "data": data,
        "signature": {"properties": properties, "checksum": checksum},
    }


def test_verify_webhook_signature_accepts_valid_checksum():
    provider = WompiProvider()
    payload = _signed_payload(reference="NEX-1-2026", status="APPROVED")
    assert provider.verify_webhook_signature(payload, CREDENTIALS) is True


def test_verify_webhook_signature_rejects_tampered_payload():
    provider = WompiProvider()
    payload = _signed_payload(reference="NEX-1-2026", status="APPROVED")
    payload["data"]["transaction"]["status"] = "DECLINED"  # payload mutado tras firmar
    assert provider.verify_webhook_signature(payload, CREDENTIALS) is False


def test_verify_webhook_signature_rejects_missing_secret():
    provider = WompiProvider()
    payload = _signed_payload(reference="NEX-1-2026", status="APPROVED")
    no_secret = ProviderCredentialsData(public_key="p", private_key="p", integrity_secret="i", events_secret="")
    assert provider.verify_webhook_signature(payload, no_secret) is False


def test_parse_webhook_event_maps_status_to_kind():
    provider = WompiProvider()
    payload = _signed_payload(reference="NEX-1-2026", status="APPROVED")
    event = provider.parse_webhook_event(payload)

    assert event is not None
    assert event.kind == "approved"
    assert event.reference == "NEX-1-2026"
    assert event.provider_transaction_id == "tx_1"


def test_parse_webhook_event_ignores_other_event_types():
    provider = WompiProvider()
    event = provider.parse_webhook_event({"event": "nonce_created", "data": {}})
    assert event is None


# ---------------------------------------------------------------------
# API directa (build_payment_init / charge) -- httpx_mock intercepta las
# llamadas salientes a Wompi, no hay red real en los tests.
# ---------------------------------------------------------------------


async def test_build_payment_init_fetches_acceptance_tokens_and_reuses_signature_formula(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())

    provider = WompiProvider()
    payment_init = await provider.build_payment_init(
        reference="NEX-1-2026", amount_cop=50_000, currency="COP", credentials=CREDENTIALS
    )

    assert payment_init.public_key == "pub_test_123"
    assert payment_init.amount_in_cents == 5_000_000
    assert payment_init.acceptance_token == "accept_xyz"
    assert payment_init.accept_personal_auth == "personal_xyz"
    # Misma formula que el checkout de Widget (test_build_checkout_computes_integrity_signature).
    expected_signature = hashlib.sha256(b"NEX-1-2026" + b"5000000" + b"COP" + b"integrity_secret").hexdigest()
    assert payment_init.integrity_signature == expected_signature


async def test_build_payment_init_raises_provider_error_without_acceptance_token(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json={"data": {}})

    provider = WompiProvider()
    with pytest.raises(ProviderRequestError):
        await provider.build_payment_init(
            reference="NEX-1-2026", amount_cop=50_000, currency="COP", credentials=CREDENTIALS
        )


async def test_build_payment_init_raises_provider_error_on_http_failure(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, status_code=500)

    provider = WompiProvider()
    with pytest.raises(ProviderRequestError):
        await provider.build_payment_init(
            reference="NEX-1-2026", amount_cop=50_000, currency="COP", credentials=CREDENTIALS
        )


async def test_charge_creates_transaction_with_tokenized_card(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": "wompi-tx-1", "status": "PENDING", "reference": "NEX-1-2026"}},
    )

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=100_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=CardPaymentMethod(token="tok_test_123", installments=1),
    )

    assert result.provider_transaction_id == "wompi-tx-1"
    assert result.raw_status == "PENDING"

    request = httpx_mock.get_requests(url=_TRANSACTIONS_URL)[0]
    assert request.headers["Authorization"] == "Bearer prv_test_123"
    sent_body = json.loads(request.content)
    assert sent_body["amount_in_cents"] == 10_000_000
    assert sent_body["acceptance_token"] == "accept_xyz"
    assert sent_body["accept_personal_auth"] == "personal_xyz"
    assert sent_body["payment_method"] == {"type": "CARD", "token": "tok_test_123", "installments": 1}


async def test_charge_raises_provider_error_when_wompi_rejects_transaction(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=422,
        json={"error": {"type": "INPUT_VALIDATION_ERROR", "reason": "token_invalido", "messages": {}}},
    )

    provider = WompiProvider()
    with pytest.raises(ProviderRequestError):
        await provider.charge(
            reference="NEX-1-2026",
            amount_cop=100_000,
            currency="COP",
            customer_email="cliente@test.com",
            credentials=CREDENTIALS,
            payment_method=CardPaymentMethod(token="tok_test_123"),
        )


# ---------------------------------------------------------------------
# API directa -- metodos alternos (Nequi, PSE, Boton Bancolombia). Ver
# docs/PLAN_METODOS_PAGO_ALTERNOS.md (repo nexolu-pos-api) para el diseno.
# ---------------------------------------------------------------------


async def test_charge_with_nequi_sends_phone_number_and_has_no_redirect(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": "wompi-tx-2", "status": "PENDING", "reference": "NEX-1-2026"}},
    )

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=50_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=NequiPaymentMethod(phone_number="3107654321"),
    )

    assert result.provider_transaction_id == "wompi-tx-2"
    assert result.redirect_url is None

    request = httpx_mock.get_requests(url=_TRANSACTIONS_URL)[0]
    sent_body = json.loads(request.content)
    assert sent_body["payment_method"] == {"type": "NEQUI", "phone_number": "3107654321"}
    assert "customer_data" not in sent_body
    # Sin redirect_url explicito, no se manda el campo (Wompi no lo exige
    # para metodos sincronos como Nequi con cobro unico).
    assert "redirect_url" not in sent_body
    # Nequi no hace polling adicional de redirect_url: si el codigo
    # intentara pollear igual, httpx_mock fallaria por no tener registrada
    # una respuesta para esa URL -- esta asercion es solo un refuerzo extra.
    assert len(httpx_mock.get_requests()) == 2


async def test_charge_with_pse_sends_payer_and_customer_data_and_extracts_redirect_url(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={
            "data": {
                "id": "wompi-tx-3",
                "status": "PENDING",
                "reference": "NEX-1-2026",
                "payment_method": {
                    "type": "PSE",
                    "extra": {"async_payment_url": "https://sandbox.wompi.co/pse/redirect"},
                },
            }
        },
    )

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=50_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=PsePaymentMethod(
            user_type=0,
            user_legal_id_type="CC",
            user_legal_id="1099888777",
            financial_institution_code="1",
            payment_description="Suscripcion Nexolu",
            customer_full_name="Cliente De Prueba",
            customer_phone_number="3107654321",
        ),
        redirect_url="https://app.test/subscription?wompi_paid=1",
    )

    assert result.redirect_url == "https://sandbox.wompi.co/pse/redirect"

    request = httpx_mock.get_requests(url=_TRANSACTIONS_URL)[0]
    sent_body = json.loads(request.content)
    assert sent_body["payment_method"]["type"] == "PSE"
    assert sent_body["payment_method"]["financial_institution_code"] == "1"
    # customer_data va HERMANO de payment_method en el body de Wompi, no anidado.
    assert sent_body["customer_data"] == {"phone_number": "3107654321", "full_name": "Cliente De Prueba"}
    # Sin esto, Wompi no sabe a donde volver tras el pago en el sitio del banco.
    assert sent_body["redirect_url"] == "https://app.test/subscription?wompi_paid=1"


async def test_charge_with_pse_polls_for_redirect_url_when_not_immediately_available(httpx_mock, monkeypatch):
    # Wompi no siempre trae extra.async_payment_url en la respuesta inicial
    # de POST /transactions -- hay que consultar GET /transactions/{id}
    # hasta que aparezca (ver _poll_for_redirect_url). Se acelera el
    # intervalo a 0 para no dormir de verdad en el test.
    monkeypatch.setattr(wompi_module, "_ASYNC_REDIRECT_POLL_INTERVAL_SECONDS", 0)

    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={
            "data": {
                "id": "wompi-tx-4",
                "status": "PENDING",
                "reference": "NEX-1-2026",
                "payment_method": {"type": "PSE"},
            }
        },
    )
    poll_url = f"{_TRANSACTIONS_URL}/wompi-tx-4"
    httpx_mock.add_response(url=poll_url, method="GET", json={"data": {"payment_method": {"extra": {}}}})
    httpx_mock.add_response(
        url=poll_url,
        method="GET",
        json={
            "data": {
                "payment_method": {"extra": {"async_payment_url": "https://sandbox.wompi.co/pse/redirect"}}
            }
        },
    )

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=50_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=PsePaymentMethod(
            user_type=0,
            user_legal_id_type="CC",
            user_legal_id="1099888777",
            financial_institution_code="1",
            payment_description="Suscripcion Nexolu",
            customer_full_name="Cliente De Prueba",
            customer_phone_number="3107654321",
        ),
    )

    assert result.redirect_url == "https://sandbox.wompi.co/pse/redirect"
    poll_request = httpx_mock.get_requests(url=poll_url)[0]
    assert poll_request.headers["Authorization"] == "Bearer prv_test_123"


async def test_charge_with_bancolombia_transfer_returns_none_redirect_when_never_provided(httpx_mock, monkeypatch):
    monkeypatch.setattr(wompi_module, "_ASYNC_REDIRECT_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(wompi_module, "_ASYNC_REDIRECT_POLL_ATTEMPTS", 2)

    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={
            "data": {
                "id": "wompi-tx-5",
                "status": "PENDING",
                "reference": "NEX-1-2026",
                "payment_method": {"type": "BANCOLOMBIA_TRANSFER"},
            }
        },
    )
    poll_url = f"{_TRANSACTIONS_URL}/wompi-tx-5"
    httpx_mock.add_response(url=poll_url, method="GET", json={"data": {"payment_method": {"extra": {}}}})
    httpx_mock.add_response(url=poll_url, method="GET", json={"data": {"payment_method": {"extra": {}}}})

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=50_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=BancolombiaTransferPaymentMethod(
            payment_description="Suscripcion Nexolu",
            ecommerce_url="https://pos.nexolu.co/subscription?paid=1",
        ),
    )

    # No revento, ni inventa una URL: el consumidor sigue esperando el webhook.
    assert result.redirect_url is None

    request = httpx_mock.get_requests(url=_TRANSACTIONS_URL)[0]
    sent_body = json.loads(request.content)
    # Wompi lo exige aunque el ejemplo principal de su doc no lo muestra --
    # confirmado con un 422 real contra sandbox sin este campo (ver
    # _payment_method_payload en providers/wompi.py).
    assert sent_body["payment_method"]["user_type"] == "PERSON"


async def test_list_payment_methods_filters_to_what_this_core_can_orchestrate(httpx_mock):
    httpx_mock.add_response(
        url=_MERCHANT_URL,
        json={
            "data": {
                "accepted_payment_methods": [
                    "BANCOLOMBIA_TRANSFER",
                    "NEQUI",
                    "PSE",
                    "CARD",
                    "BANCOLOMBIA_COLLECT",
                    "DAVIPLATA",
                ],
            }
        },
    )

    provider = WompiProvider()
    methods = await provider.list_payment_methods(credentials=CREDENTIALS)

    assert set(methods) == {"CARD", "NEQUI", "PSE", "BANCOLOMBIA_TRANSFER"}


async def test_list_pse_financial_institutions_normalizes_wompi_fields(httpx_mock):
    httpx_mock.add_response(
        url="https://sandbox.wompi.co/v1/pse/financial_institutions",
        method="GET",
        json={
            "data": [
                {"financial_institution_code": "1", "financial_institution_name": "Banco que aprueba"},
                {"financial_institution_code": "2", "financial_institution_name": "Banco que declina"},
            ]
        },
    )

    provider = WompiProvider()
    institutions = await provider.list_pse_financial_institutions(credentials=CREDENTIALS)

    assert institutions[0].code == "1"
    assert institutions[0].name == "Banco que aprueba"
    assert institutions[1].code == "2"

    request = httpx_mock.get_requests(url="https://sandbox.wompi.co/v1/pse/financial_institutions")[0]
    assert request.headers["Authorization"] == "Bearer pub_test_123"


async def test_list_pse_financial_institutions_raises_provider_error_on_http_failure(httpx_mock):
    httpx_mock.add_response(url="https://sandbox.wompi.co/v1/pse/financial_institutions", status_code=500)

    provider = WompiProvider()
    with pytest.raises(ProviderRequestError):
        await provider.list_pse_financial_institutions(credentials=CREDENTIALS)


# ---------------------------------------------------------------------
# Fuentes de pago (tokenizar tarjeta/Nequi PARA REUSO, a diferencia de un
# charge() de una sola vez). Ver docs/PLAN_METODOS_PAGO_ALTERNOS.md (repo
# nexolu-pos-api) seccion 9.
# ---------------------------------------------------------------------


async def test_create_payment_source_for_card_uses_private_key(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_PAYMENT_SOURCES_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": 3891, "type": "CARD", "status": "AVAILABLE"}},
    )

    provider = WompiProvider()
    source = await provider.create_payment_source(
        credentials=CREDENTIALS, source_type="CARD", token="tok_test_123", customer_email="cliente@test.com"
    )

    assert source.id == "3891"
    assert source.type == "CARD"
    assert source.status == "AVAILABLE"

    request = httpx_mock.get_requests(url=_PAYMENT_SOURCES_URL)[0]
    # Fuentes de pago exigen la llave PRIVADA (a diferencia de tokenizar,
    # que usa la publica) -- confirmado contra docs.wompi.co, es lo que
    # fuerza a que este paso viva en el Core y no en el frontend.
    assert request.headers["Authorization"] == "Bearer prv_test_123"
    sent_body = json.loads(request.content)
    assert sent_body["type"] == "CARD"
    assert sent_body["token"] == "tok_test_123"
    assert sent_body["acceptance_token"] == "accept_xyz"


async def test_create_payment_source_for_nequi(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_PAYMENT_SOURCES_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": 4200, "type": "NEQUI", "status": "AVAILABLE"}},
    )

    provider = WompiProvider()
    source = await provider.create_payment_source(
        credentials=CREDENTIALS,
        source_type="NEQUI",
        token="nequi_test_xxx",
        customer_email="cliente@test.com",
    )

    assert source.id == "4200"
    assert source.type == "NEQUI"

    sent_body = json.loads(httpx_mock.get_requests(url=_PAYMENT_SOURCES_URL)[0].content)
    assert sent_body["type"] == "NEQUI"
    assert sent_body["token"] == "nequi_test_xxx"


async def test_create_payment_source_raises_provider_error_when_wompi_declines(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_PAYMENT_SOURCES_URL,
        method="POST",
        status_code=422,
        json={"error": {"type": "UNPROCESSABLE", "reason": "La fuente de pago ha sido declinada"}},
    )

    provider = WompiProvider()
    with pytest.raises(ProviderRequestError):
        await provider.create_payment_source(
            credentials=CREDENTIALS, source_type="CARD", token="tok_declined", customer_email="cliente@test.com"
        )


async def test_void_payment_source(httpx_mock):
    httpx_mock.add_response(
        url=f"{_PAYMENT_SOURCES_URL}/3891/void",
        method="PUT",
        json={"data": {"id": 3891, "type": "CARD", "status": "VOIDED"}},
    )

    provider = WompiProvider()
    source = await provider.void_payment_source(credentials=CREDENTIALS, payment_source_id="3891")

    assert source.status == "VOIDED"
    request = httpx_mock.get_requests(url=f"{_PAYMENT_SOURCES_URL}/3891/void")[0]
    assert request.headers["Authorization"] == "Bearer prv_test_123"


async def test_charge_with_payment_source_sends_source_id_as_sibling_field(httpx_mock):
    httpx_mock.add_response(url=_MERCHANT_URL, json=_merchant_response())
    httpx_mock.add_response(
        url=_TRANSACTIONS_URL,
        method="POST",
        status_code=201,
        json={"data": {"id": "wompi-tx-source-1", "status": "PENDING", "reference": "NEX-1-2026"}},
    )

    provider = WompiProvider()
    result = await provider.charge(
        reference="NEX-1-2026",
        amount_cop=65_000,
        currency="COP",
        customer_email="cliente@test.com",
        credentials=CREDENTIALS,
        payment_method=PaymentSourceChargeMethod(payment_source_id="3891", installments=3),
    )

    assert result.provider_transaction_id == "wompi-tx-source-1"

    sent_body = json.loads(httpx_mock.get_requests(url=_TRANSACTIONS_URL)[0].content)
    # payment_source_id va HERMANO de payment_method, no un `type` adentro.
    assert sent_body["payment_source_id"] == "3891"
    assert sent_body["payment_method"] == {"installments": 3}
    assert "type" not in sent_body["payment_method"]
