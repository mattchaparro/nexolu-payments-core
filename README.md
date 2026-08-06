# Nexolu Payments Core

Pasarela de pagos unificada para todo el ecosistema Nexolu (POS, IA Core y lo
que venga despues). No es "los pagos del POS": es un servicio Python/FastAPI
independiente, agnostico de cual app lo llama, que cualquier producto puede
usar sin acoplarse a el ni a los demas -- mismo espiritu que `nexolu-ia-core`.

Hoy procesa pagos con **Wompi** (unico proveedor implementado), pero el resto
del servicio programa contra una interfaz de proveedor (`providers/base.py`),
no contra Wompi directamente: agregar otro proveedor manana es una clase
nueva, no una reescritura.

## Principios de arquitectura

- **El Core nunca toca la base de datos de negocio de ninguna app.** No sabe
  que es una suscripcion, un plan o un negocio -- solo sabe de
  transacciones, referencias y montos. Cada app integradora decide que
  significa un pago aprobado para ella.
- **Una API key por aplicacion, nunca por usuario final.** El Core no tiene
  sesion de usuario propia: confia en que la app que lo llama (autenticada
  con su API key) ya resolvio quien es su cliente.
- **Cada integracion es dueña de sus propias credenciales de proveedor.**
  Nada de un solo `WOMPI_PUBLIC_KEY` global en `.env` como hoy en
  pos-saas-legacy: cada app puede tener su propio merchant account de Wompi,
  configurable en base de datos, sin redeploy.
- **La fuente de verdad de un pago es el webhook del proveedor, nunca el
  navegador.** El resultado que el widget de Wompi devuelve en el cliente es
  solo UX; la transaccion no se marca aprobada hasta que Wompi lo confirma
  por su propio webhook, firmado.
- **El Core nunca importa nada de una app especifica.** Agregar una app
  integradora nueva es darla de alta en la base de datos (slug, API key,
  webhook, credenciales de Wompi) -- no toca una linea de codigo.

## Estructura

```
nexolu_payments_core/
  main.py              FastAPI app factory
  config.py             Settings (env vars): solo infraestructura, nunca credenciales de proveedor
  core/
    auth/                 Identidad de las integraciones (API key -> Integration)
    memory/                Persistencia (SQLAlchemy async) + Alembic: integrations,
                          provider_credentials, fee_schedules, transactions, webhook_deliveries
    payments/              fees.py (comision por transaccion) + service.py (orquestador)
    webhooks/               Firma y envio de las notificaciones salientes (Core -> app integradora)
    security/               Cifrado en reposo de credenciales de proveedor (Fernet)
    telemetry/              Logging JSON estructurado
  providers/            Contrato agnostico de proveedor + implementacion de Wompi
  api/v1/               Endpoints HTTP: payments (lo que la app CONSUME), webhooks (lo que Wompi llama)
scripts/
  register_integration.py  CLI para dar de alta/actualizar una integracion en BD
tests/                 pytest (fees, firma de Wompi, flujo completo de pago via ASGI)
alembic/               Migraciones de esquema
docs/
  APP_INTEGRATION.md    Guia de integracion para una app nueva
```

## Como correr en local

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
# Generar y pegar PAYMENTS_MASTER_KEY en .env:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

alembic upgrade head    # o dejar que arranque solo con SQLite (ver main.py)
uvicorn nexolu_payments_core.main:app --reload
```

## Dar de alta una integracion (app cliente)

No hay panel de administracion todavia. Se hace con el script incluido, que
crea/actualiza la integracion, sus credenciales de Wompi y su tarifa en la
base de datos:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"  # generar api-key y webhook-secret

python -m scripts.register_integration \
    --slug pos-legacy --name "Nexolu POS" \
    --api-key <api-key-generada> \
    --webhook-url https://pos.nexolu.co/integrations/payments-core/webhook \
    --webhook-secret <webhook-secret-generado> \
    --wompi-public-key pub_prod_xxx --wompi-private-key prv_prod_xxx \
    --wompi-integrity-secret xxx --wompi-events-secret xxx \
    --environment production
```

El comando imprime la URL de webhook que hay que configurar en el dashboard
de Wompi de esa integracion: `/v1/webhooks/wompi/<slug>`.

## Probar el flujo completo

```bash
curl -X POST http://localhost:8000/v1/payments/intents \
  -H "Authorization: Bearer <api-key-de-la-integracion>" \
  -H "Content-Type: application/json" \
  -d '{
    "reference": "NEX-1-2026",
    "amount_cop": 50000,
    "redirect_url": "https://pos.nexolu.co/subscription/billing?paid=1",
    "customer": {"email": "cliente@nexolu.co", "full_name": "Cliente Demo"}
  }'
```

Devuelve los parametros para que el frontend de la app abra el widget de
Wompi (`checkout.wompi.co/widget.js`) -- ver `docs/APP_INTEGRATION.md`
seccion 2 para el snippet completo. Cuando Wompi confirma el pago, notifica
al Core en `/v1/webhooks/wompi/<slug>`, y el Core reenvia el resultado ya
normalizado (agnostico de Wompi) al `webhook_url` de la integracion.

## Tests

```bash
pytest
```

Cubren: calculo de comision (paridad con `WompiFees` del legacy, incluido el
redondeo estilo PHP), verificacion de firma de Wompi (payload valido,
manipulado, sin secreto), y el flujo de punta a punta via ASGI: crear un
intent, simular el webhook de Wompi, verificar que la transaccion queda
aprobada con su comision calculada y que el Core notifica a la app
integradora con un webhook firmado.

## Extender el Core

- **Agregar un proveedor de pago nuevo** (PayU, Stripe...): escribir una
  clase que cumpla `PaymentProvider` (`providers/base.py`) en un modulo
  nuevo de `providers/` y darla de alta en `providers/registry.py`. Nada
  mas del Core cambia.
- **Agregar una aplicacion nueva**: `python -m scripts.register_integration`
  con sus propios slug/API key/webhook/credenciales de Wompi. El Core no
  cambia.
- **Trabajo futuro conocido** (documentado, no implementado): los
  reintentos de webhook saliente son sincronos dentro del request del
  webhook entrante (ver `core/webhooks/dispatcher.py`) -- una cola de
  trabajo (Celery/RQ/arq) para reintentos en background y un endpoint de
  "reenviar a mano" son el siguiente paso natural cuando el volumen lo
  justifique. Tampoco hay reconciliacion activa contra la API REST de
  Wompi (solo se confia en el webhook, igual que hoy pos-saas-legacy);
  `GET /v1/payments/transactions/{reference}` cubre el caso de polling
  desde el frontend, pero no hay un job que le pregunte a Wompi por
  transacciones que se quedaron `pending` demasiado tiempo.
