from __future__ import annotations

import hashlib

from nexolu_payments_core.providers.base import ProviderCredentialsData
from nexolu_payments_core.providers.wompi import WompiProvider

CREDENTIALS = ProviderCredentialsData(
    public_key="pub_test_123",
    private_key="prv_test_123",
    integrity_secret="integrity_secret",
    events_secret="events_secret",
)


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
