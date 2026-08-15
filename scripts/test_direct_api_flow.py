"""Script de prueba manual del flujo de API directa (flow="api") contra un
Nexolu Payments Core corriendo LOCAL y las credenciales de SANDBOX de Wompi
ya registradas con `scripts/register_integration.py`.

No es parte de la suite de pytest (esa ya cubre el flujo con mocks) -- esto
es para probarlo de punta a punta con la API real de Wompi sandbox, igual
que harias con curl/Postman, pero sin escribir cada paso a mano.

Soporta los 4 metodos de pago que el Core sabe orquestar (ver
docs/APP_INTEGRATION.md seccion 2b): CARD (default), NEQUI, PSE y
BANCOLOMBIA_TRANSFER.

Uso (tarjeta, como antes):
    python -m scripts.test_direct_api_flow \\
        --base-url http://localhost:8000 \\
        --api-key <tu api_key registrada> \\
        --amount-cop 50000

Uso (Nequi):
    python -m scripts.test_direct_api_flow --api-key <tu api_key> \\
        --payment-method nequi --nequi-phone 3991111111

Uso (PSE -- consulta el banco disponible en vivo via el Core):
    python -m scripts.test_direct_api_flow --api-key <tu api_key> \\
        --payment-method pse --pse-bank-code 1 --poll-seconds 30

Uso (Boton Bancolombia):
    python -m scripts.test_direct_api_flow --api-key <tu api_key> \\
        --payment-method bancolombia_transfer --poll-seconds 30

Con una tarjeta que empiece en "4242..." Wompi la aprueba en sandbox; con
"4111..." la declina. Con Nequi, el celular "3991111111" aprueba y
"3992222222" declina. Con PSE, el codigo de banco "1" aprueba y "2" declina
(ver docs.wompi.co, "Test Data for Sandbox").

Sobre el webhook: este script NO recibe el webhook de Wompi (tu servidor
local no es alcanzable desde internet a menos que lo expongas con algo como
ngrok/cloudflared y cargues esa URL publica + tu slug en el dashboard de
Wompi: `https://<tu-tunel>/v1/webhooks/wompi/<tu-slug>`). Sin eso, la
transaccion se va a quedar en "pending" para siempre en tu base local
aunque Wompi si la haya procesado -- eso es esperado, no un bug: es
exactamente la razon por la que el estado local NO se actualiza con la
respuesta sincrona del charge (ver docs/APP_INTEGRATION.md).

Para PSE/Boton Bancolombia especificamente: la respuesta del charge puede
traer `redirect_url` (el Core hace un polling corto contra Wompi para
conseguirlo, ver providers/wompi.py) -- este script la imprime si aparece,
pero no la abre en un navegador (es un script de terminal).
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
        "--payment-method",
        choices=["card", "nequi", "pse", "bancolombia_transfer"],
        default="card",
        help="Metodo de pago a probar (default: card).",
    )
    parser.add_argument(
        "--card-number",
        default="4242424242424242",
        help="4242... => APPROVED en sandbox. 4111... => DECLINED. Solo aplica con --payment-method card.",
    )
    parser.add_argument("--card-cvc", default="123")
    parser.add_argument("--card-exp-month", default="12")
    parser.add_argument("--card-exp-year", default="29")
    parser.add_argument("--card-holder", default="Cliente De Prueba", help="Tambien usado como nombre del pagador PSE.")
    parser.add_argument(
        "--nequi-phone",
        default="3991111111",
        help="3991111111 => APPROVED en sandbox. 3992222222 => DECLINED. Solo aplica con --payment-method nequi.",
    )
    parser.add_argument(
        "--pse-bank-code",
        default=None,
        help="Codigo de GET /v1/payments/pse/financial-institutions. Si se omite, se usa el primero disponible.",
    )
    parser.add_argument("--pse-user-legal-id", default="1099888777")
    parser.add_argument("--customer-phone", default="3107654321", help="Usado por PSE (dato del pagador).")
    parser.add_argument(
        "--poll-seconds", type=int, default=0, help="Si es >0, hace polling a /transactions ese tiempo esperando el webhook (requiere tunel publico configurado en Wompi)."
    )
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

    if args.payment_method == "card":
        payment_method = _build_card_payment_method(args, headers=headers, wompi_base=wompi_base, public_key=public_key)
    elif args.payment_method == "nequi":
        print(f"\n== 2. Metodo NEQUI: celular {args.nequi_phone} (sin pasos previos) ==")
        payment_method = {"type": "NEQUI", "phone_number": args.nequi_phone}
    elif args.payment_method == "pse":
        payment_method = _build_pse_payment_method(args, base_url=args.base_url, headers=headers)
    else:
        print("\n== 2. Metodo BANCOLOMBIA_TRANSFER (sin pasos previos) ==")
        payment_method = {
            "type": "BANCOLOMBIA_TRANSFER",
            "payment_description": f"Prueba Nexolu {reference}",
            "ecommerce_url": "https://example.test/billing",
        }

    print(f"\n== 3. Cobrando el intent ({args.payment_method}) ==")
    with httpx.Client(timeout=30) as client:
        charge_resp = client.post(
            f"{args.base_url}/v1/payments/intents/{reference}/charge",
            headers=headers,
            json={"payment_method": payment_method},
        )
    _print_response(charge_resp)
    if charge_resp.status_code != 200:
        sys.exit(1)

    redirect_url = charge_resp.json().get("redirect_url")
    if redirect_url:
        print(f"\nredirect_url disponible: {redirect_url}")
        print("(el usuario tendria que terminar el pago ahi -- este script no abre navegador)")

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


def _build_card_payment_method(args: argparse.Namespace, *, headers: dict, wompi_base: str, public_key: str) -> dict:
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
    return {"type": "CARD", "token": card_token, "installments": 1}


def _build_pse_payment_method(args: argparse.Namespace, *, base_url: str, headers: dict) -> dict:
    bank_code = args.pse_bank_code
    if bank_code is None:
        print("\n== 2. Consultando bancos PSE disponibles via el Core (GET /v1/payments/pse/financial-institutions) ==")
        with httpx.Client(timeout=30) as client:
            banks_resp = client.get(f"{base_url}/v1/payments/pse/financial-institutions", headers=headers)
        _print_response(banks_resp)
        if banks_resp.status_code != 200:
            sys.exit(1)
        institutions = banks_resp.json()["financial_institutions"]
        if not institutions:
            print("Wompi no devolvio ningun banco PSE disponible.")
            sys.exit(1)
        bank_code = institutions[0]["code"]
        print(f"Usando el primer banco de la lista: {institutions[0]}")
    else:
        print(f"\n== 2. Metodo PSE: banco {bank_code} (pasado por --pse-bank-code) ==")

    return {
        "type": "PSE",
        "user_type": 0,
        "user_legal_id_type": "CC",
        "user_legal_id": args.pse_user_legal_id,
        "financial_institution_code": bank_code,
        "payment_description": "Prueba Nexolu Payments Core",
        "customer_full_name": args.card_holder,
        "customer_phone_number": args.customer_phone,
    }


def _print_response(response: httpx.Response) -> None:
    print(f"HTTP {response.status_code}")
    try:
        import json

        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)


if __name__ == "__main__":
    main()
