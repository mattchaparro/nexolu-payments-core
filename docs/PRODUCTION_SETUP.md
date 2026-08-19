# Poner un medio de pago en producción

Runbook operativo: cómo llevar una integración (app cliente + su comercio de
Wompi) de sandbox a producción. No es una guía de arquitectura — para eso ver
`APP_INTEGRATION.md` y `MULTI_MERCHANT_ARCHITECTURE.md`. Para el cutover del
droplet/infra en general (no específico de pagos), ver
`nexolu-pos-api/docs/PRODUCTION_CUTOVER.md`.

## 0. Antes de empezar

- **Llaves de producción de Wompi** (`pub_prod_...`/`prv_prod_...`) — Wompi
  las entrega recién después de verificar el comercio (proceso propio de
  ellos, aparte de este repo). Las llaves `_test_` de sandbox nunca sirven
  acá, ni por error: el proveedor las detecta por el prefijo
  (`_base_url_for()` en `providers/wompi.py`) y las manda a
  `sandbox.wompi.co` igual, así que un merchant "de producción" con llaves
  de test simplemente nunca procesaría plata real (fallaría silenciosamente
  en ese sentido, no con un error obvio) — confirmar el prefijo antes de
  cargarlas.
- **`payments-core` ya desplegado** en el droplet (`deploy.sh` de este repo:
  pull + rebuild + `alembic upgrade head`), con:
  - `DATABASE_URL` apuntando a MySQL (nunca SQLite en producción — el
    autocreate de tablas en `main.py` está gateado explícitamente a
    `sqlite://`).
  - `PAYMENTS_MASTER_KEY` y `PROVISIONING_KEY` **propios de producción**,
    generados de nuevo — nunca reusar los de sandbox/local. Ver README de
    este repo para el comando de generación de cada uno.
  - DNS + TLS resueltos para el dominio público de este servicio (el mismo
    que usa `deploy.sh` para el healthcheck, p.ej. `payments.nexolu.co`).

## 1. Provisioning: Merchant + Integration + credenciales Wompi

Con el `PROVISIONING_KEY` de producción (nunca el de sandbox/local):

```bash
PROV_KEY="<PROVISIONING_KEY de produccion>"
BASE_URL="https://payments.nexolu.co"

# 1. Merchant (una vez por empresa dueña de la cuenta de Wompi - si el
#    merchant ya existe de sandbox, es el MISMO merchant: los ambientes
#    sandbox/production son un campo de la Integration y de la credencial
#    Wompi, no del Merchant).
curl -s -X POST "$BASE_URL/v1/admin/merchants" \
  -H "X-Payments-Provisioning-Key: $PROV_KEY" -H "Content-Type: application/json" \
  -d '{"name":"Nexolu POS","slug":"nexolu-pos"}'
# -> guardar el "id" de la respuesta (merchant_id)

# 2. Integration en environment=production (si ya existe la de sandbox,
#    esta es una fila NUEVA y distinta - una Integration es por
#    merchant+slug, no por merchant+environment, asi que el slug tiene que
#    ser distinto, p.ej. "nexolu-pos" para produccion, algo como
#    "nexolu-pos-sandbox" si se quiere conservar la de pruebas).
curl -s -X POST "$BASE_URL/v1/admin/merchants/$MERCHANT_ID/integrations" \
  -H "X-Payments-Provisioning-Key: $PROV_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Nexolu POS",
    "slug": "nexolu-pos",
    "environment": "production",
    "webhook_url": "https://api.nexolu.co/api/webhooks/payments-core",
    "widget_enabled": false
  }'
# -> la respuesta trae "api_key" y "webhook_secret" - se muestran UNA SOLA
#    VEZ, no hay forma de volver a pedirlos despues (ni un "regenerar" hoy).
#    Guardarlos ya mismo en el gestor de secretos que uses, antes de cerrar
#    la terminal.

# 3. Credenciales Wompi de PRODUCCION para ese merchant
curl -s -X POST "$BASE_URL/v1/admin/merchants/$MERCHANT_ID/providers/wompi" \
  -H "X-Payments-Provisioning-Key: $PROV_KEY" -H "Content-Type: application/json" \
  -d '{
    "environment": "production",
    "public_key": "pub_prod_...",
    "private_key": "prv_prod_...",
    "integrity_secret": "...",
    "events_secret": "..."
  }'
```

`widget_enabled` arranca en `false` a propósito (ver `payments.py` /
`Integration.widget_enabled`) — si la app va a usar el flujo Widget legado
(`checkout.wompi.co/widget.js`) además de `flow=api`, prenderlo explícito:

```bash
curl -s -X PATCH "$BASE_URL/v1/admin/merchants/$MERCHANT_ID/integrations/$INTEGRATION_ID" \
  -H "X-Payments-Provisioning-Key: $PROV_KEY" -H "Content-Type: application/json" \
  -d '{"widget_enabled": true}'
```

## 2. Configurar el webhook en el dashboard de Wompi

Un único endpoint para todos los comercios (el Core resuelve merchant e
integration por la `reference` de la transacción, no por la URL — ver
`APP_INTEGRATION.md` sección 5):

```
https://payments.nexolu.co/v1/webhooks/wompi
```

Cargarlo en el dashboard de Wompi del comercio de **producción** (dashboard
separado del de sandbox). Sin esto, los pagos reales nunca confirman — el
Core no tiene forma de enterarse de que Wompi aprobó/rechazó nada.

Verificar también, en ese mismo dashboard, qué `accepted_payment_methods`
tiene habilitado el comercio real (Wompi puede tenerlos negociados aparte
por comercio) — `GET /v1/payments/payment-methods` solo puede mostrar la
intersección entre lo que Wompi habilitó y lo que este Core sabe orquestar
(`_SUPPORTED_PAYMENT_METHODS` en `providers/wompi.py`), nunca más que eso.
Si falta Nequi/PSE/Botón Bancolombia en la lista y el comercio sí los tiene
contratados con Wompi, el síntoma va a ser "no aparece el botón" en el
frontend, no un error.

## 3. Configurar la app cliente (ej. `nexolu-pos-api`)

En el `.env` de producción de la app:

```bash
PAYMENTS_CORE_API_KEY=<api_key del paso 1.2>
PAYMENTS_CORE_BASE_URL=https://payments.nexolu.co
PAYMENTS_CORE_WEBHOOK_SECRET=<webhook_secret del paso 1.2>
```

`PAYMENTS_CORE_BASE_URL` en producción es la URL pública HTTPS real del
servicio (a diferencia de local, donde apunta a `host.docker.internal` por
el contenedor Sail — ver comentario en `.env.example` de `nexolu-pos-api`).
**Sin confirmar todavía:** si `nexolu-infra` pone ambos servicios en la
misma red interna de Docker, puede que un hostname interno
(`http://payments-core:8000` o similar) sea más apropiado que salir a
internet y volver a entrar — revisar el `docker-compose.yml` de
`nexolu-infra` al desplegar, no asumir la URL pública ciegamente.

## 4. Tablas nuevas agregadas después del primer deploy

Si la app cliente usa el patrón de `database/legacy-schema/patches/` (ver
`nexolu-pos-api`) para tablas 100% nuevas que no vienen del dump original
(ej. `business_payment_sources`, `billing_profiles`) — correr
`php artisan schema:apply-patches` como parte del deploy, **siempre**, no
solo la primera vez. Ya es parte de `deploy.sh` de `nexolu-pos-api`, pero
si alguna vez se agrega una tabla nueva relacionada con pagos ahí, el patch
correspondiente tiene que ir en el mismo commit que el cambio de
`schema.sql` — ver `database/legacy-schema/patches/README.md` de ese repo
para las reglas exactas. Ya pasó una vez (`billing_profiles` se agregó a
`schema.sql` sin su patch, encontrado recién al correr la suite completa)
— no asumir que "ya está en schema.sql" alcanza para un ambiente que ya
estaba provisionado antes de ese commit.

## 5. Verificación

```bash
curl -s https://payments.nexolu.co/health
# {"status":"ok"}

curl -s https://payments.nexolu.co/v1/payments/payment-methods \
  -H "Authorization: Bearer <PAYMENTS_CORE_API_KEY de produccion>"
# {"provider":"wompi","accepted_payment_methods":[...],"widget_enabled":...}
```

Y funcional, de punta a punta, con una tarjeta/cuenta real (montos chicos):
iniciar un cobro real desde la app, confirmar que Wompi manda el webhook a
`/v1/webhooks/wompi`, y que el Core reenvía el evento normalizado al
`webhook_url` de la Integration — revisar los logs de ambos lados
(`payments-core` y la app cliente) para el mismo `reference`.

## Referencias

- `APP_INTEGRATION.md` — contrato completo que consume una app cliente.
- `MULTI_MERCHANT_ARCHITECTURE.md` — cómo se relacionan Merchant/Integration/credenciales.
- `nexolu-pos-api/docs/PRODUCTION_CUTOVER.md` — runbook general del droplet (no específico de pagos).
- `nexolu-utils/build/start_local_pos.sh` — el mismo provisioning pero automatizado para sandbox/local; sirve de referencia de los mismos curls, solo que apuntando a `localhost:8003` con el `PROVISIONING_KEY` local.
