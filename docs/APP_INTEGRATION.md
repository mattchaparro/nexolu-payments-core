# Integrar una aplicacion nueva al Nexolu Payments Core

Guia de referencia para conectar una app (pos-saas-legacy, nexolu-pos-api,
nexolu-ia-core o lo que venga) a la pasarela de pagos unificada. El Core
nunca sabe que es una "suscripcion" o un "negocio": solo procesa
transacciones con una `reference` que tu app genera y te notifica el
resultado. Toda la logica de negocio (activar algo, mandar un correo, lo que
sea) sigue siendo tuya.

Hay dos formas de completar un cobro con tarjeta, elegidas con el campo
`flow` al crear el intent (seccion 2). **Ambas terminan de la misma forma**:
la fuente de verdad del resultado SIEMPRE es el webhook del Core (seccion
3), nunca lo que devuelve el navegador ni la respuesta sincrona de ningun
endpoint.

- **`flow: "api"` (recomendado, nuevo).** Tu app nunca sale de tu propia UI:
  tu frontend tokeniza la tarjeta hablando directo con Wompi (nunca con el
  Core) y tu backend confirma el cobro con `POST /intents/{reference}/charge`.
  Ver seccion 2b.
- **`flow: "widget"` (default, legado -- sigue funcionando igual que
  siempre).** Tu frontend abre el widget hospedado por Wompi con los
  parametros que te devuelve el Core. Ver seccion 2a. No hay fecha de
  apagado todavia; si ya integraste este flujo no tienes que migrar.

Para exploracion interactiva, con el servicio corriendo local (`uvicorn
nexolu_payments_core.main:app --reload`) el contrato completo, tipado, con
ejemplos, esta siempre en `http://localhost:8000/docs` (Swagger UI) y
`http://localhost:8000/redoc` -- esta guia es la version narrada del mismo
contrato.

## 1. Arquitectura en una frase

```
flow="api" (recomendado):

Usuario final → tu app → POST /v1/payments/intents {flow: "api"} (Core)
                   ▲                                    │
                   │                        Core pide a Wompi los tokens
                   │                        de aceptacion legal (Wompi API)
                   │                                    ▼
       tu frontend tokeniza  ←──────────  respuesta con payment_init
       la tarjeta DIRECTO con Wompi
       (nunca con el Core / tu backend)
                   │
                   │ tu backend recibe el card token
                   ▼
       POST /v1/payments/intents/{reference}/charge (Core)
                   │
                   │              Core llama a Wompi API (POST /transactions)
                   ▼                          │
       ack inmediato (aun "pending")  ←───────┘
                   │
                   │  ... Wompi procesa async ...
                   ▼
          Wompi confirma  →  POST /v1/webhooks/wompi/<tu-slug> (Core)
                                        │
                          Core verifica firma, calcula tu comision,
                          marca la transaccion aprobada/rechazada
                                        │
                                        ▼
                        POST {tu webhook_url} (Core → tu app, FIRMADO)
                                        │
                          tu app activa lo que corresponda


flow="widget" (legado, sigue funcionando):

Usuario final → tu app → POST /v1/payments/intents (Core)
                   ▲                    │
                   │        Core arma el checkout de Wompi (local, sin red)
                   │                    │
          tu frontend abre              ▼
          el widget de Wompi  ←── respuesta con checkout params
                   │
                   │ el usuario paga (Wompi, nunca tu app, ve la tarjeta)
                   ▼
          Wompi confirma  →  POST /v1/webhooks/wompi/<tu-slug> (Core)
                                        │
                          (mismo procesamiento de webhook que arriba)
```

En ambos flujos, el Core nunca recibe el numero de tarjeta: en `api` porque
tu frontend tokeniza directo con Wompi; en `widget` porque el que ve la
tarjeta es el iframe de Wompi, nunca tu app ni el Core.

## 2. Que debes CONSUMIR de tu lado (iniciar un cobro)

### `POST /v1/payments/intents`

Autenticado con tu API key: `Authorization: Bearer <tu api_key>`.

```json
{
  "reference": "NEX-42-20260806-AB12",
  "amount_cop": 50000,
  "currency": "COP",
  "redirect_url": "https://tu-app.com/billing?paid=1",
  "customer": { "email": "cliente@correo.com", "full_name": "Nombre Cliente" },
  "metadata": { "business_id": "42", "subscription_days": 30 },
  "flow": "api"
}
```

- **`reference`**: la generas TU, unica dentro de tu integracion (igual que
  hoy `SubscriptionController::wompiInitiate()` arma
  `NEX-<business_id>-<timestamp>-<random>` en pos-saas-legacy). Es lo que
  usas para conciliar tu propia orden con el resultado.
- **`metadata`**: cualquier dato tuyo (ids internos, dias de suscripcion...)
  que quieras recibir de vuelta tal cual en el webhook saliente. El Core no
  la interpreta, solo la guarda y la reenvia.
- **`flow`**: `"api"` o `"widget"` (default si se omite). Determina que
  version de la respuesta te interesa -- ver 2a/2b. El campo `redirect_url`
  sigue siendo obligatorio incluso con `flow: "api"` (se usa igual para el
  checkout legado que el Core sigue calculando en paralelo, sin costo).

Errores posibles (ambos flujos): `401` (API key invalida), `409` (ya existe
una transaccion con esa `reference`), `503` (tu integracion no tiene
credenciales de Wompi activas -- avisale a quien administra el Core), `502`
(solo con `flow: "api"`: Wompi no respondio al pedir los tokens de
aceptacion legal -- reintenta).

### 2a. Widget Checkout (legado, `flow: "widget"` o sin `flow`)

Respuesta (`201`):

```json
{
  "transaction_id": "a1b2c3...",
  "reference": "NEX-42-20260806-AB12",
  "provider": "wompi",
  "status": "pending",
  "checkout": {
    "public_key": "pub_prod_xxx",
    "amount_in_cents": 5000000,
    "currency": "COP",
    "reference": "NEX-42-20260806-AB12",
    "integrity_signature": "...",
    "redirect_url": "https://tu-app.com/billing?paid=1",
    "customer_data": { "email": "cliente@correo.com", "full_name": "Nombre Cliente" }
  }
}
```

Tu frontend usa `checkout` tal cual para abrir el widget de Wompi -- **es el
mismo widget que ya usa `Billing.vue` de pos-saas-legacy hoy**, la unica
diferencia es que los parametros vienen del Core en vez de tu propio
backend:

```js
const script = document.createElement('script');
script.src = 'https://checkout.wompi.co/widget.js';
document.head.appendChild(script);
script.onload = () => {
  const checkout = new window.WidgetCheckout({
    currency: data.checkout.currency,
    amountInCents: data.checkout.amount_in_cents,
    reference: data.checkout.reference,
    publicKey: data.checkout.public_key,
    signature: { integrity: data.checkout.integrity_signature },
    redirectUrl: data.checkout.redirect_url,
    customerData: data.checkout.customer_data,
  });
  checkout.open((result) => { /* solo UX: la confirmacion real llega por tu webhook */ });
};
```

### 2b. API directa (nuevo, `flow: "api"`)

Respuesta (`201`) -- trae ademas el bloque `payment_init`:

```json
{
  "transaction_id": "a1b2c3...",
  "reference": "NEX-42-20260806-AB12",
  "provider": "wompi",
  "status": "pending",
  "checkout": { "...": "el legado sigue viniendo, por si lo necesitas de fallback" },
  "payment_init": {
    "public_key": "pub_prod_xxx",
    "amount_in_cents": 5000000,
    "currency": "COP",
    "reference": "NEX-42-20260806-AB12",
    "integrity_signature": "...",
    "acceptance_token": "eyJhbGciOi...",
    "accept_personal_auth": "eyJhbGciOi..."
  }
}
```

**Paso 1 -- tu frontend tokeniza la tarjeta DIRECTO con Wompi** (nunca con
tu backend ni con el Core: el numero de tarjeta no debe tocar ninguno de
los dos). Antes de tokenizar, muestrale al usuario los textos legales de
`acceptance_token`/`accept_personal_auth` (Wompi exige un checkbox
explicito de aceptacion -- ver "Acceptance tokens" en la documentacion de
Wompi):

```js
const response = await fetch(`https://production.wompi.co/v1/tokens/cards`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${data.payment_init.public_key}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    number: '4242424242424242',
    cvc: '123',
    exp_month: '12',
    exp_year: '29',
    card_holder: 'Nombre Cliente',
  }),
});
const { data: { id: cardToken } } = await response.json();
```

(Usa `https://sandbox.wompi.co/v1/...` mientras pruebas con llaves
`pub_test_...`/`prv_test_...`.)

**Paso 2 -- tu backend confirma el cobro con el Core**, mandandole el
`cardToken` del paso anterior:

### `POST /v1/payments/intents/{reference}/charge`

```json
{
  "payment_method": { "type": "CARD", "token": "tok_prod_...", "installments": 1 }
}
```

Respuesta (`200`):

```json
{
  "transaction_id": "a1b2c3...",
  "reference": "NEX-42-20260806-AB12",
  "status": "pending",
  "provider_transaction_id": "wompi-tx-id",
  "provider_status": "PENDING"
}
```

**`status` se queda en `"pending"` a proposito**, aunque `provider_status`
ya diga `APPROVED`/`DECLINED`: es solo el ack inmediato de Wompi, no la
confirmacion. La confirmacion real -- la que dispara tu webhook y calcula tu
comision -- sigue llegando exclusivamente por
`POST /v1/webhooks/wompi/<tu-slug>` (seccion 3), exactamente igual que en el
flujo Widget. Usa `GET /v1/payments/transactions/{reference}` (abajo) para
hacer polling de UX mientras esperas.

Errores posibles: `404` (no hay una transaccion `pending` con esa
`reference` -- el intent no se creo con `flow: "api"`, o ya se cobro antes),
`503` (integracion sin credenciales activas), `502` (Wompi rechazo el
intento de cobro -- p.ej. token invalido o expirado; la transaccion queda
marcada `error` en el Core, no se queda "pending" para siempre esperando un
webhook que nunca va a llegar porque Wompi nunca acepto la transaccion).

### `GET /v1/payments/transactions/{reference}`

Mismo uso en ambos flujos: tu frontend puede consultar esto mientras espera
que tu webhook confirme, como fallback de UX si el webhook tarda (equivalente
al polling que hoy hace `Billing.vue` contra `/subscription/status`).

```json
{
  "transaction_id": "...", "reference": "NEX-42-20260806-AB12", "provider": "wompi",
  "status": "approved", "amount_cop": 50000, "currency": "COP",
  "fee_cop": 2410, "net_amount_cop": 47590,
  "provider_transaction_id": "wompi-tx-id", "created_at": "...", "confirmed_at": "..."
}
```

## 3. Que debes IMPLEMENTAR de tu lado (el webhook)

Un unico endpoint tuyo, publico, que reciba `POST` del Core cuando una de
tus transacciones cambia de estado -- **identico sin importar si la
transaccion se origino con `flow: "widget"` o `flow: "api"`**:

```json
{
  "event": "payment.approved",
  "integration": "pos-legacy",
  "transaction_id": "a1b2c3...",
  "reference": "NEX-42-20260806-AB12",
  "provider": "wompi",
  "provider_transaction_id": "wompi-tx-id",
  "amount_cop": 50000,
  "currency": "COP",
  "fee_cop": 2410,
  "net_amount_cop": 47590,
  "status": "approved",
  "occurred_at": "2026-08-06T12:00:00",
  "metadata": { "business_id": "42", "subscription_days": 30 }
}
```

`event` es uno de `payment.approved`, `payment.declined`, `payment.error`,
`payment.voided`, `payment.pending` -- agnostico de que el proveedor por
debajo sea Wompi (u otro, el dia que se agregue uno) y de si el pago se
origino por Widget o por API directa. Tu logica de negocio programa contra
este contrato, no contra el formato de Wompi.

### Verificar la firma (obligatorio)

Cada request trae `X-Nexolu-Signature` y `X-Nexolu-Timestamp`. La firma es
`HMAC-SHA256("{timestamp}.{body crudo}", tu_webhook_secret)` -- el
`webhook_secret` que te dieron al registrar tu integracion. Verificarla
**antes** de procesar nada, exactamente con el mismo espiritu que
`WompiService::verifyWebhookSignature()` ya hace hoy con el webhook de
Wompi:

```php
// Laravel / pos-saas-legacy
public function handle(Request $request): JsonResponse
{
    $timestamp = $request->header('X-Nexolu-Timestamp');
    $signature = $request->header('X-Nexolu-Signature');
    $secret    = config('services.nexolu_payments_core.webhook_secret');

    $expected = hash_hmac('sha256', $timestamp . '.' . $request->getContent(), $secret);

    if (! hash_equals($expected, (string) $signature)) {
        return response()->json(['error' => 'invalid_signature'], 401);
    }

    $payload = $request->json()->all();
    // $payload['reference'] es TU reference: buscar tu orden por ahi,
    // igual que hoy SubscriptionCheckoutOrder::where('order_key', $reference).
    // ...

    return response()->json(['ok' => true]);
}
```

```python
# Python / FastAPI (ver core/webhooks/signing.py del Core, misma logica)
import hashlib, hmac

def verify(secret: str, raw_body: bytes, timestamp: str, signature: str) -> bool:
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### Idempotencia

El Core reintenta hasta 3 veces (inmediato, +1s, +2s) si tu endpoint no
responde `2xx`. Tu handler debe ser idempotente: si te llega el mismo
`transaction_id`/`reference` dos veces, la segunda vez no debe volver a
activar/cobrar nada -- solo responder `200` (igual que
`WompiWebhookController::activateSubscription()` ya chequea
`status === 'pending'` antes de procesar).

### Responder rapido

Responde `2xx` en cuanto verificaste la firma y encolaste/guardaste el
evento -- no hagas trabajo pesado (mandar emails, etc.) sincrono dentro del
webhook si se puede evitar. El Core espera con un timeout corto.

## 4. Configurar tu integracion

No hay panel de administracion todavia: quien administra el Core corre

```bash
python -m scripts.register_integration \
    --slug <tu-slug> --name "<Tu App>" \
    --api-key <api-key-que-vas-a-usar> \
    --webhook-url https://tu-app.com/tu-endpoint-de-webhook \
    --webhook-secret <secreto-para-verificar-la-firma> \
    --wompi-public-key ... --wompi-private-key ... \
    --wompi-integrity-secret ... --wompi-events-secret ... \
    --environment production
```

Esto crea/actualiza tu fila en `integrations` + tus credenciales de Wompi en
`provider_credentials` + tu tarifa por defecto en `fee_schedules`
(2.65% + 700 COP fijo + 19% IVA, iguales defaults que `WompiFees` del
legacy -- pedile a quien administra el Core que la ajuste si tu tarifa
negociada con Wompi es distinta). El comando imprime la URL que debes cargar
en el dashboard de Wompi de tu merchant account: `/v1/webhooks/wompi/<tu-slug>`.

`--environment` (`sandbox`/`production`) solo se usa para versionar tus
credenciales dentro del Core -- el proveedor mismo elige el ambiente de
Wompi (`sandbox.wompi.co` vs `production.wompi.co`) leyendo el prefijo de
tus propias llaves (`pub_test_`/`prv_test_` vs `pub_prod_`/`prv_prod_`), asi
que no hay nada adicional que configurar para usar `flow: "api"`.

Guarda tu `api_key` y `webhook_secret` como secretos de tu propia app (en tu
`.env`, nunca en el repo) -- son lo que te identifica ante el Core y lo que
usas para verificar que un webhook realmente vino del Core.

## 5. Checklist para integrar una app nueva

1. Pedir que te registren con `scripts/register_integration.py` (slug, API
   key, webhook URL/secret, credenciales de Wompi de tu propio merchant
   account).
2. Implementar tu endpoint de webhook: verificar firma (seccion 3), resolver
   tu propia orden por `reference`, ser idempotente, responder rapido.
3. En tu flujo de checkout, elegir un `flow`:
   - `api` (recomendado): `POST /intents {flow: "api"}` → tu frontend
     tokeniza contra Wompi con `payment_init.public_key` → `POST
     /intents/{reference}/charge` con el token (seccion 2b).
   - `widget` (legado): `POST /intents` → abrir el widget de Wompi con
     `checkout` (seccion 2a).
4. Opcional: usar `GET /v1/payments/transactions/{reference}` para polling
   de UX mientras esperas tu webhook.
5. Probar de punta a punta con las credenciales de **sandbox** de Wompi
   (`pub_test_...`/`prv_test_...`) antes de pasar a `--environment production`.

`core/` del Core no cambia en ningun paso de esta lista.

## 6. Trabajo futuro conocido

- No hay todavia un endpoint de reconciliacion activa contra
  `GET /transactions/{id}` de Wompi (ver README, "Extender el Core") --
  si una transaccion se queda `pending` mucho tiempo despues de un `charge`
  exitoso (ack inmediato recibido pero el webhook nunca llega), hoy no hay
  un job que le pregunte a Wompi por su estado real.
- `flow: "api"` por ahora solo soporta `payment_method.type: "CARD"`. Nequi
  y PSE via API directa (Wompi los soporta, con flujos de confirmacion
  distintos -- push notification y redireccion al banco respectivamente) no
  estan implementados: usa `flow: "widget"` para esos metodos por ahora.

## 7. Validacion de `flow: "api"` (2026-08-14)

- **Suite automatizada**: `pytest -v` -- 29/29 pasan, incluidos los tests
  nuevos de `build_payment_init`/`charge` (`tests/test_wompi_provider.py`,
  con `httpx_mock`) y del flujo completo (`tests/test_payments_flow.py`,
  `test_create_intent_flow_api_*` / `test_charge_intent_*`).
- **Extremo a extremo contra Wompi sandbox real** (no mocks), con
  `scripts/test_direct_api_flow.py`, servidor local + tunel publico
  (cloudflared) recibiendo el webhook real de Wompi en
  `/v1/webhooks/wompi/<slug>`:
  - Tarjeta `4242...` (aprobada): `POST /intents {flow: "api"}` devolvio
    `payment_init` con tokens de aceptacion reales; tokenizacion directa
    contra `sandbox.wompi.co` OK; `POST /intents/{reference}/charge` devolvio
    ack inmediato (`provider_status: "PENDING"`, `status` local
    `"pending"`); el webhook real de Wompi llego y la transaccion paso a
    `status: "approved"` con `fee_cop`/`net_amount_cop` calculados
    correctamente (mismos parametros de tarifa que el flujo Widget).
  - Tarjeta `4111...` (declinada): mismo flujo, la transaccion termino en
    `status: "declined"` con `fee_cop`/`net_amount_cop` en `null` (no se
    cobra comision sobre transacciones declinadas).
- Con esto quedan verificados de punta a punta: la firma de integridad, la
  obtencion de tokens de aceptacion legal, la creacion de la transaccion en
  Wompi con tarjeta tokenizada, la verificacion de firma del webhook
  entrante, y el calculo de comision -- todo igual que en el flujo Widget,
  como establece la seccion 1.
