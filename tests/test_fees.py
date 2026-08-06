from __future__ import annotations

from nexolu_payments_core.core.payments.fees import calculate_fee_cop


def test_matches_wompi_fees_default_rates():
    # Mismos defaults que App\Support\WompiFees del POS legacy: 2.65% + 700
    # COP fijo, +19% IVA sobre ese subtotal.
    fee = calculate_fee_cop(100_000, percent_fee=2.65, fixed_fee_cop=700, iva_percent=19)
    # base = 100000*0.0265 + 700 = 2650 + 700 = 3350; *1.19 = 3986.5 -> 3987
    assert fee == 3987


def test_fee_is_per_transaction_not_over_aggregate():
    # Sumar el fee de 2 pagos de 50000 no es lo mismo que calcularlo una
    # vez sobre 100000 -- el fijo se aplica por transaccion.
    per_tx = calculate_fee_cop(50_000, percent_fee=2.65, fixed_fee_cop=700, iva_percent=19)
    aggregate = calculate_fee_cop(100_000, percent_fee=2.65, fixed_fee_cop=700, iva_percent=19)
    assert per_tx * 2 != aggregate
