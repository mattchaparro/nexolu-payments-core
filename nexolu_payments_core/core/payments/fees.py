"""Calculo de la comision que retiene el proveedor por una transaccion.

Misma formula que `App\\Support\\WompiFees` del POS legacy: se aplica POR
TRANSACCION, no sobre un monto agregado -- el fee fijo es por cobro
individual, asi que sumar el fee de 10 pagos pequenos da un numero distinto
(mayor) que aplicar la formula una sola vez sobre la suma de esos 10 pagos.
Quien use esto para un periodo debe iterar cada transaccion y sumar el
resultado, no sumar los montos primero.
"""
from __future__ import annotations

import math


def calculate_fee_cop(amount_cop: int, *, percent_fee: float, fixed_fee_cop: float, iva_percent: float) -> int:
    base_fee = amount_cop * (percent_fee / 100) + fixed_fee_cop
    total = base_fee * (1 + iva_percent / 100)

    # PHP round() (WompiFees del legacy) redondea la mitad siempre hacia
    # arriba; el round() de Python redondea la mitad al par mas cercano
    # (banker's rounding). Con los valores tipicos de Wompi eso puede diferir
    # en 1 peso -- se replica el comportamiento de PHP a proposito para que
    # el fee calculado aca cuadre centavo a centavo con el legacy.
    return math.floor(total + 0.5)
