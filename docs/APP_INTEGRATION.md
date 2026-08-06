# Integrar una aplicacion nueva al Nexolu Payments Core

Guia de referencia para conectar una app (pos-saas-legacy, nexolu-pos-api,
nexolu-ia-core o lo que venga) a la pasarela de pagos unificada. El Core
nunca sabe que es una "suscripcion" o un "negocio": solo procesa
transacciones con una `reference` que tu app genera y te notifica el
resultado. Toda la logica de negocio (activar algo, mandar un correo, lo que
sea) sigue siendo tuya.

## 1. Arquitectura en una frase

```
Usuario final → tu app (Laravel/Node/lo que sea) → POST /v1/payments/intents (Core)
                     ▲                                          │
                     │                              Core arma el checkout de Wompi
                     │                                          │
              tu frontend abre                                  ▼
              el widget de Wompi  ←──────────────────  respuesta con checkout params
                     │
                     │ el usuario paga (Wompi, nunca tu app, ve la tarjeta)
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
```

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
  "metadata": { "business_id": "42", "subscription_days": 30 }
}
```

- **`reference`**: la generas TU, unica dentro de tu integracion (igual que
  hoy `SubscriptionController::wompiInitiate()` arma
  `NEX-<business_id>-<timestamp>-<random>` en pos-saas-legacy). Es lo que
  usas para conciliar tu propia orden con el resultado.
- **`metadata`**: cualquier dato tuyo (ids internos, dias de suscripcion...)
  que quieras recibir de vuelta tal cual en el webhook saliente. El Core no
  la interpreta, solo la guarda y la reenvia.

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

Errores posibles: `401` (API key invalida), `409` (ya existe una transaccion
con esa `reference`), `503` (tu integracion no tiene credenciales de Wompi
activas -- avisale a quien administra el Core).

### `GET /v1/payments/transactions/{reference}`

Mismo uso que el polling que hoy hace `Billing.vue` contra
`/subscription/status`: tu frontend puede consultar esto mientras espera que
tu webhook confirme, como fallback de UX si el webhook tarda.

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
tus transacciones cambia de estado:

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
debajo sea Wompi (u otro, el dia que se agregue uno). Tu logica de negocio
programa contra este contrato, no contra el formato de Wompi.

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

Guarda tu `api_key` y `webhook_secret` como secretos de tu propia app (en tu
`.env`, nunca en el repo) -- son lo que te identifica ante el Core y lo que
usas para verificar que un webhook realmente vino del Core.

## 5. Checklist para integrar una app nueva

1. Pedir que te registren con `scripts/register_integration.py` (slug, API
   key, webhook URL/secret, credenciales de Wompi de tu propio merchant
   account).
2. Implementar tu endpoint de webhook: verificar firma (seccion 3), resolver
   tu propia orden por `reference`, ser idempotente, responder rapido.
3. En tu flujo de checkout: llamar `POST /v1/payments/intents`, abrir el
   widget de Wompi con la respuesta (seccion 2).
4. Opcional: usar `GET /v1/payments/transactions/{reference}` para polling
   de UX mientras esperas tu webhook.
5. Probar de punta a punta con las credenciales de **sandbox** de Wompi
   antes de pasar a `--environment production`.

`core/` del Core no cambia en ningun paso de esta lista.
