from __future__ import annotations

import hashlib
import json

import pytest

from nexolu_payments_core.providers.base import (
    CardPaymentMethod,
    ProviderCredentialsData,
    ProviderRequestError,
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
