# Nexolú Payments Core

Servicio FastAPI de orquestación de pagos para las aplicaciones del ecosistema Nexolú y para aplicaciones de terceros.

Wompi es el primer proveedor implementado. El Core programa contra `PaymentProvider`, por lo que proveedores futuros como Bold no deben requerir una reescritura del corazón del sistema.

## Arquitectura

```text
Merchant
├── ProviderCredential
│   └── Wompi
├── Integration: POS
└── Integration: Spa

Merchant: Colegio
├── ProviderCredential
│   └── Wompi
└── Integration: Colegio
```

- **Merchant**: empresa propietaria de las cuentas de pago.
- **Integration**: aplicación autorizada a consumir el Core.
- **ProviderCredential**: credenciales cifradas del proveedor pertenecientes al Merchant.
- **Transaction**: contexto completo de un pago, incluyendo `merchant_id` e `integration_id`.

## API keys

Payments Core genera automáticamente la API key y el webhook secret al crear una Integration. Una aplicación usa:

```http
Authorization: Bearer <integration-api-key>
```

La API key no debe escribirse en el código fuente del consumidor.

## Provisioning

La configuración inicial se realiza mediante endpoints protegidos por `PROVISIONING_KEY`.

```http
POST /v1/admin/merchants
POST /v1/admin/merchants/{merchant_id}/integrations
POST /v1/admin/merchants/{merchant_id}/providers/wompi
```

`PROVISIONING_KEY` es un secreto de servidor y nunca debe exponerse a un frontend público. El frontend administrativo futuro debe hablar con un backend autenticado que controle estas operaciones.

## Payments

Crear un intent:

```http
POST /v1/payments/intents
Authorization: Bearer <integration-api-key>
```

La aplicación envía monto, cliente y metadata, pero **no genera la reference**.

Payments Core genera una referencia como `pay_<unique-id>`, la persiste y la envía exactamente igual al proveedor.

```json
{
  "amount_cop": 50000,
  "currency": "COP",
  "customer": {"email": "cliente@example.com"},
  "metadata": {"order_id": "12345"},
  "flow": "api"
}
```

Respuesta conceptual:

```json
{
  "transaction_id": "...",
  "reference": "pay_...",
  "provider": "wompi",
  "status": "pending"
}
```

## Webhook Wompi

Todos los comercios pueden usar el mismo endpoint:

```http
POST /v1/webhooks/wompi
```

Wompi envía la `reference`. El Core encuentra la Transaction por el índice de `reference`, obtiene directamente `merchant_id` e `integration_id`, carga las credenciales Wompi del Merchant, valida la firma y actualiza el pago.

No se necesita `/wompi/{integration_slug}`.

## Webhook hacia la aplicación

Después de procesar el evento del proveedor, Payments Core envía un evento normalizado a `Integration.webhook_url`, firmado con el `webhook_secret` de esa Integration.

El payload es agnóstico del proveedor e incluye `transaction_id`, `reference`, `provider`, `provider_transaction_id`, monto, estado y metadata.

## Seguridad

Las credenciales privadas de proveedores se almacenan cifradas mediante Fernet. La clave maestra `PAYMENTS_MASTER_KEY` solo vive en el entorno del servicio.

Nunca se devuelven private keys, integrity secrets o events secrets mediante endpoints de consulta.

## Desarrollo

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn nexolu_payments_core.main:app --reload
```

Generar una Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Definir también `PROVISIONING_KEY` en el entorno para usar los endpoints de provisioning.

## Tests

```bash
pytest
```

Los tests deben cubrir especialmente aislamiento por Merchant, generación de references, credenciales cifradas, firma de webhooks, idempotencia y routing de webhook por Transaction.

## Extensibilidad

```text
Application
    ↓
Payments Core
    ↓
PaymentProvider
├── Wompi
├── Bold (future)
└── other providers
```

Agregar un proveedor debe limitarse a implementar el contrato de `providers/base.py` y registrarlo en `providers/registry.py`.

Consulta `docs/APP_INTEGRATION.md` y `docs/MULTI_MERCHANT_ARCHITECTURE.md` para los contratos actuales.
