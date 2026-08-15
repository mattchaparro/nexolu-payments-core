"""Script de prueba manual del flujo de API directa (flow="api") contra un
Nexolu Payments Core corriendo LOCAL y las credenciales de SANDBOX de Wompi
ya registradas con `scripts/register_integration.py`.

No es parte de la suite de pytest (esa ya cubre el flujo con mocks) -- esto
es para probarlo de punta a punta con la API real de Wompi sandbox, igual
que harias con curl/Postman, pero sin escribir cada paso a mano.

Uso:
    python -m scripts.test_direct_api_flow \\
        --base-url http://localhost:8000 \\
        --api-key <tu api_key registrada> \\
        --amount-cop 50000

Con un `--card-number` que empiece en "4242..." Wompi la aprueba en
sandbox; con una que empiece en "4111..." la declina (ver docs.wompi.co,
"Test Data for Sandbox") -- por defecto usa la de aprobacion.

Sobre el webhook: este script NO recibe el webhook de Wompi (tu servidor
local no es alcanzable desde internet a menos que lo expongas con algo como
ngrok/cloudflared y cargues esa URL publica + tu slug en el dashboard de
Wompi: `https://<tu-tunel>/v1/webhooks/wompi/<tu-slug>`). Sin eso, la
transaccion se va a quedar en "pending" para siempre en tu base local
aunque Wompi si la haya procesado -- eso es esperado, no un bug: es
exactamente la razon por la que el estado local NO se actualiza con la
respuesta sincrona del charge (ver docs/APP_INTEGRATION.md).
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000", help="Tu Nexolu Payments Core local.")
    parser.add_argument("--api-key", required=True, help="api_key de la integracion (register_integration.py).")
    parser.add_argument("--reference", default=None, help="Por defecto se genera una nueva cada vez.")
    parser.add_argument("--amount-cop", type=int, default=50_000)
    parser.add_argument("--customer-email", default="test@nexolu.co")
    parser.add_argument(
        "--card-number",
        default="4242424242424242",
        help="4242... => APPROVED en sandbox. 4111... => DECLINED. Ver docs.wompi.co.",
    )
    parser.add_argument("--card-cvc", default="123")
    parser.add_argument("--card-exp-month", default="12")
    parser.add_argument("--card-exp-year", default="29")
    parser.add_argument("--card-holder", default="Cliente De Prueba")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Si es >0, hace polling a /transactions ese tiempo esperando el webhook (requiere tunel publico configurado en Wompi).")
    args = parser.parse_args()

    reference = args.reference or f"TEST-{uuid.uuid4().hex[:12].upper()}"
    headers = {"Authorization": f"Bearer {args.api_key}"}

    print(f"== 1. Creando intent (flow=api) reference={reference} ==")
    with httpx.Client(timeout=30) as client:
        intent_resp = client.post(
            f"{args.base_url}/v1/payments/intents",
            headers=headers,
            json={
                "reference": reference,
                "amount_cop": args.amount_cop,
                "currency": "COP",
                "redirect_url": "https://example.test/billing",
                "customer": {"email": args.customer_email, "full_name": args.card_holder},
                "flow": "api",
            },
        )
    _print_response(intent_resp)
    if intent_resp.status_code != 201:
        sys.exit(1)

    payment_init = intent_resp.json()["payment_init"]
    public_key = payment_init["public_key"]
    wompi_base = "https://sandbox.wompi.co/v1" if "_test_" in public_key else "https://production.wompi.co/v1"

    print(f"\n== 2. Tokenizando tarjeta DIRECTO con Wompi ({wompi_base}) -- el Core no ve el numero ==")
    with httpx.Client(timeout=30) as client:
        token_resp = client.post(
            f"{wompi_base}/tokens/cards",
            headers={"Authorization": f"Bearer {public_key}"},
            json={
                "number": args.card_number,
                "cvc": args.card_cvc,
                "exp_month": args.card_exp_month,
                "exp_year": args.card_exp_year,
                "card_holder": args.card_holder,
            },
        )
    _print_response(token_resp)
    if token_resp.status_code >= 300:
        sys.exit(1)
    card_token = token_resp.json()["data"]["id"]

    print(f"\n== 3. Cobrando el intent con el token ya generado ==")
    with httpx.Client(timeout=30) as client:
        charge_resp = client.post(
            f"{args.base_url}/v1/payments/intents/{reference}/charge",
            headers=headers,
            json={"payment_method": {"type": "CARD", "token": card_token, "installments": 1}},
        )
    _print_response(charge_resp)
    if charge_resp.status_code != 200:
        sys.exit(1)

    if args.poll_seconds <= 0:
        print(
            "\n(No se hizo polling. El status se va a quedar 'pending' en el Core hasta que "
            "llegue el webhook real de Wompi -- necesitas exponer tu servidor local con un tunel "
            "publico, ver el docstring de este script.)"
        )
        return

    print(f"\n== 4. Polling GET /transactions/{reference} por hasta {args.poll_seconds}s esperando el webhook ==")
    deadline = time.monotonic() + args.poll_seconds
    with httpx.Client(timeout=30) as client:
        while time.monotonic() < deadline:
            status_resp = client.get(f"{args.base_url}/v1/payments/transactions/{reference}", headers=headers)
            status = status_resp.json().get("status")
            print(f"  status actual: {status}")
            if status != "pending":
                _print_response(status_resp)
                return
            time.sleep(2)

    print("Se agoto el tiempo de espera sin que llegara el webhook -- revisa tu tunel/config de eventos en Wompi.")


def _print_response(response: httpx.Response) -> None:
    print(f"HTTP {response.status_code}")
    try:
        import json

        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)


if __name__ == "__main__":
    main()
