# Handoff: migración Wompi Widget → Wompi API directa

Resumen de sesión para continuar el trabajo en Claude Code (terminal local).
Contexto completo: se implementó un flujo de pago por API directa de Wompi
que **coexiste** con el flujo de Widget existente (no lo reemplaza), en
`nexolu-payments-core`. Este documento resume qué se hizo, qué falta
verificar, y el procedimiento exacto para registrar una integración de
prueba y correr un test real contra el sandbox de Wompi.

## 1. Qué se implementó

Objetivo: que el Core pueda iniciar/gestionar el pago hablando directo con
la API de Wompi (`docs.wompi.co`), sin depender del Widget/iframe de Wompi,
manteniendo la arquitectura y contratos existentes (`PaymentProvider`,
orquestador en `core/payments/service.py`, persistencia de
`Integration`/`ProviderCredential`, webhook entrante como fuente de verdad).

**Decision de diseño clave: coexistencia, no reemplazo.** `build_checkout`
(Widget) se dejo intacto. Se agregaron dos capacidades nuevas al contrato
`PaymentProvider` (`providers/base.py`):

- `async def build_payment_init(...)`: pide a Wompi los tokens de
  aceptacion legal (`GET /merchants/:public_key`) + calcula la firma de
  integridad (misma formula que el Widget, local). Es `async` porque a
  diferencia de `build_checkout`, SI requiere una llamada de red.
- `async def charge(...)`: crea la transaccion en Wompi
  (`POST /transactions`) usando una tarjeta YA TOKENIZADA por el frontend
  de la app (nunca por el Core). Wompi confirma. Vuelve un `ChargeResult`
  (ack inmediato -- NO cambia el estado local de la transaccion).

**Regla de oro respetada: el webhook sigue siendo la unica fuente de
verdad.** Aunque `charge()` reciba un status sincrono de Wompi
(`APPROVED`/`DECLINED`), la transaccion en el Core se queda `pending` hasta
que llega `POST /v1/webhooks/wompi/<slug>` -- exactamente igual que en el
flujo Widget. Excepcion: si Wompi rechaza el intento de cobro con un error
de red/4xx/5xx (`ProviderRequestError`), no va a haber webhook nunca para
esa transaccion -- ahi si se marca `error` de inmediato (ver
`service.charge_payment_intent`).

**Como se elige sandbox vs produccion:** el proveedor infiere el ambiente
del PREFIJO de la llave (`pub_test_`/`prv_test_` => `sandbox.wompi.co`,
`pub_prod_`/`prv_prod_` => `production.wompi.co`). No se toco
`ProviderCredentialsData` ni el modelo de `ProviderCredential` para esto.

### Endpoints HTTP nuevos/cambiados (`api/v1/payments.py`)

- `POST /v1/payments/intents`: acepta un campo nuevo opcional `flow`
  (`"widget"` default, `"api"`). Con `flow` omitido o `"widget"`, la
  respuesta es IDENTICA a como era antes (cero cambios para integraciones
  existentes). Con `flow: "api"`, la respuesta trae ademas un bloque
  `payment_init` (public_key, acceptance_token, accept_personal_auth,
  integrity_signature, amount_in_cents, currency, reference).
- `POST /v1/payments/intents/{reference}/charge` (nuevo): recibe
  `{"payment_method": {"type": "CARD", "token": "...", "installments": 1}}`
  (el token lo genero el frontend de la app tokenizando DIRECTO con Wompi,
  usando `payment_init.public_key` -- el Core nunca ve el numero de
  tarjeta). Responde con el ack inmediato de Wompi; el `status` de la
  transaccion se queda en `"pending"`.
- `GET /v1/payments/transactions/{reference}`: sin cambios, sirve para
  polling en ambos flujos.

### Archivos modificados

```
nexolu_payments_core/providers/base.py       # contrato extendido (async)
nexolu_payments_core/providers/wompi.py      # llamadas HTTP reales a Wompi
nexolu_payments_core/core/payments/service.py # create_payment_intent(flow=) + charge_payment_intent()
nexolu_payments_core/api/v1/payments.py       # flow= + POST /intents/{reference}/charge
tests/test_wompi_provider.py                  # tests de build_payment_init/charge con httpx_mock
tests/test_payments_flow.py                   # tests e2e del flujo API directa
docs/APP_INTEGRATION.md                       # guia actualizada (seccion 2b nueva)
scripts/test_direct_api_flow.py               # NUEVO: script de prueba manual contra sandbox real
```

No se tocaron: `core/webhooks/*`, `core/memory/*`, `providers/registry.py`,
`scripts/register_integration.py`, `main.py`.

## 2. ✅ Verificado (2026-08-14, en sesión de Claude Code local)

La sesión de Cowork que hizo estos cambios corrió en un contenedor cloud
con acceso a red restringido a una lista blanca de dominios (ni siquiera
`pypi.org` era alcanzable, y `sandbox.wompi.co` tampoco) — por lo tanto en
esa sesión solo se pudo verificar que los archivos compilaban
(`python -m py_compile`) y revisar la lógica manualmente.

Eso ya se completó en una sesión posterior de Claude Code local:

- **`pytest -v`**: 29/29 pasan, incluidos los tests nuevos
  (`test_build_payment_init_*`, `test_charge_*` en
  `tests/test_wompi_provider.py`; `test_create_intent_flow_api_*`,
  `test_charge_intent_*` en `tests/test_payments_flow.py`). No hizo falta
  arreglar nada.
- **Llamadas reales a Wompi sandbox**: se registró una integración de
  prueba, se levantó el servidor local + túnel público (cloudflared) y se
  corrió `scripts/test_direct_api_flow.py` de punta a punta, con tarjeta
  aprobada (`4242...`) y declinada (`4111...`). Ambos casos: el webhook real
  de Wompi llegó por el túnel y el estado local pasó correctamente de
  `pending` a `approved`/`declined`, con `fee_cop`/`net_amount_cop`
  calculados igual que en el flujo Widget. Detalle completo en
  `docs/APP_INTEGRATION.md`, sección 7.

Nota para quien retome esto: hacía falta generar `PAYMENTS_MASTER_KEY` en
`.env` (estaba vacía) para que `register_integration.py` pudiera cifrar las
credenciales — sin esa clave el registro falla con un `RuntimeError`
explícito, no en silencio.

## 3. Cómo registrar una integración de prueba (sandbox)

Requiere credenciales sandbox reales de una cuenta comercio de Wompi
(dashboard de Wompi, modo sandbox → `pub_test_...`, `prv_test_...`,
`test_integrity_...`, `test_events_...`). Estas credenciales son las mismas
que se necesitarían para el flujo Widget, no hay nada nuevo que pedirle a
Wompi por esta feature.

```powershell
cd "C:\Users\jrodr\OneDrive\Documentos\Nexolu\nexolu-payments-core"
.venv\Scripts\Activate.ps1

python -m scripts.register_integration `
    --slug test-local --name "Prueba Local" `
    --api-key mi-api-key-de-prueba `
    --webhook-url https://webhook.site/<id-que-te-da-webhook.site> `
    --webhook-secret cualquier-secreto `
    --wompi-public-key pub_test_xxx --wompi-private-key prv_test_xxx `
    --wompi-integrity-secret xxx --wompi-events-secret xxx `
    --environment sandbox
```

### Aclaración importante sobre las URLs (para no confundir, ya nos pasó en esta sesión)

Hay **tres direcciones de tráfico distintas**, cada una con su propia URL:

| # | Quién llama a quién | URL / dónde se configura |
|---|---|---|
| 1 | tu app → Core | `http://localhost:8000` (o el túnel, si pruebas contra un frontend externo) |
| 2 | **Wompi → Core** (el webhook entrante que confirma el pago) | URL pública del **túnel** (ngrok/cloudflared), configurada **en el dashboard de Wompi**, sección Eventos: `https://<tunel>/v1/webhooks/wompi/<slug>` |
| 3 | Core → tu app (el webhook saliente ya normalizado) | El `--webhook-url` de `register_integration.py`. Para pruebas, `https://webhook.site/<id>` sirve tal cual (ya es público, no necesita túnel porque es el Core quien llama hacia afuera, no al revés) |

El `--webhook-url` de `register_integration.py` **NO tiene nada que ver**
con el túnel de Cloudflare/ngrok ni con la URL que se configura en el
dashboard de Wompi. Son cosas independientes.

## 4. Levantar el servidor + túnel para el webhook de Wompi

```powershell
# Terminal 1
uvicorn nexolu_payments_core.main:app --reload

# Terminal 2 (dejar corriendo mientras se prueba)
winget install --id Cloudflare.cloudflared   # una sola vez
cloudflared tunnel --url http://localhost:8000
```

`cloudflared` imprime una URL tipo `https://random-words-1234.trycloudflare.com`.
Esa URL cambia cada vez que se reinicia el túnel (es efímero). Con esa URL,
entrar al dashboard de Wompi (comercio, modo sandbox) → sección
Eventos/Webhooks → pegar:

```
https://random-words-1234.trycloudflare.com/v1/webhooks/wompi/test-local
```

(reemplazando `test-local` por el `--slug` real usado al registrar).

Alternativa sin instalar nada nuevo si ya se tiene cuenta: `ngrok http 8000`
(requiere `ngrok config add-authtoken` una vez).

## 5. Correr el test end-to-end contra Wompi sandbox real

Ya existe `scripts/test_direct_api_flow.py` (agregado en esta sesión) que
automatiza: crear intent (`flow=api`) → tokenizar una tarjeta de prueba
DIRECTO contra Wompi → cobrar (`POST /intents/{reference}/charge`) →
opcionalmente hacer polling esperando el webhook real.

```powershell
python -m scripts.test_direct_api_flow `
    --api-key mi-api-key-de-prueba `
    --poll-seconds 30
```

Tarjetas de prueba de Wompi sandbox (ver `docs.wompi.co`, "Test Data for
Sandbox"): `4242 4242 4242 4242` → `APPROVED`; `4111 1111 1111 1111` →
`DECLINED`; CVC y fecha de expiración pueden ser cualquier valor válido
futuro. El script usa la de aprobación por defecto (`--card-number` para
cambiarla).

Si todo está bien conectado (servidor local corriendo, túnel activo, URL
correcta en el dashboard de Wompi con el slug correcto), la consola del
script debería mostrar el `status` pasando de `pending` a `approved` (o
`declined`) durante el polling. Si se agota el tiempo sin cambiar, revisar
en este orden: 1) el túnel sigue corriendo y no cambió de URL, 2) la URL en
el dashboard de Wompi tiene el slug correcto al final, 3) `webhook.site` (o
donde apunte `--webhook-url`) también recibió algo — si SÍ llegó ahí pero
`GET /transactions/{reference}` sigue en `pending`, el problema está entre
Wompi y el túnel, no en el Core.

## 6. Siguientes pasos sugeridos para Claude Code

1. ✅ `pytest -v` — confirmado, 29/29 pasan (ver sección 2).
2. ✅ Ejecutar el flujo de la sección 5 contra sandbox real al menos una vez
   (aprobado y declinado) y confirmar que `fee_cop`/`net_amount_cop` se
   calculan igual que en el flujo Widget para el mismo monto — confirmado
   (ver sección 2 y `docs/APP_INTEGRATION.md` sección 7).
3. Revisar si conviene mover la generación de tokens de aceptación
   (`build_payment_init`/`charge`) a un caché corto (hoy se piden en vivo a
   Wompi en cada llamada) si el volumen lo justifica — quedó documentado
   como decisión deliberada por seguridad/simplicidad, no como omisión.
4. Decidir si se quiere generar y versionar un `docs/openapi.json` estático
   (comando ya documentado en `docs/APP_INTEGRATION.md`/conversación
   previa) o si el Swagger UI en vivo (`/docs`) es suficiente.
5. Una vez confirmado en sandbox, definir el plan de rollout a producción
   (nada especial requerido del lado del Core: mismas credenciales
   `_prod_` ya seleccionan el ambiente productivo automáticamente).
