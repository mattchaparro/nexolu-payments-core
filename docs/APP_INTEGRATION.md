# Integrating an application with Nexolú Payments Core

## 1. Concepts

A **Merchant** is the company that owns the payment-provider account.

An **Integration** is an application that consumes Payments Core.

A Merchant can have many Integrations and they share the Merchant's provider credentials.

```text
Merchant A
├── Wompi
├── POS Integration
└── Spa Integration
```

A different company has a different Merchant and therefore different Wompi credentials.

## 2. Provisioning

Provisioning is performed by a trusted backend using the server-side `PROVISIONING_KEY`.

### Create Merchant

```http
POST /v1/admin/merchants
X-Payments-Provisioning-Key: <provisioning-key>
Content-Type: application/json
```

```json
{
  "name": "Colegio San X",
  "slug": "colegio-san-x"
}
```

### Create Integration

```http
POST /v1/admin/merchants/{merchant_id}/integrations
X-Payments-Provisioning-Key: <provisioning-key>
```

```json
{
  "name": "Colegio App",
  "slug": "colegio-app",
  "environment": "production",
  "webhook_url": "https://example.com/payments/webhook"
}
```

Payments Core generates the Integration's `api_key` and `webhook_secret`. The credentials are returned only during creation and must be stored by the trusted application backend.

### Configure Wompi

```http
POST /v1/admin/merchants/{merchant_id}/providers/wompi
X-Payments-Provisioning-Key: <provisioning-key>
```

```json
{
  "environment": "production",
  "public_key": "pub_prod_...",
  "private_key": "prv_prod_...",
  "integrity_secret": "...",
  "events_secret": "..."
}
```

Private provider secrets are encrypted with the Core's Fernet master key before being persisted.

## 3. Creating a payment

Applications authenticate with their Integration API key:

```http
Authorization: Bearer <integration-api-key>
```

Create an intent:

```http
POST /v1/payments/intents
```

```json
{
  "amount_cop": 50000,
  "currency": "COP",
  "redirect_url": "https://example.com/payment/result",
  "customer": {
    "email": "customer@example.com"
  },
  "metadata": {
    "order_id": "12345"
  },
  "flow": "api"
}
```

The application does **not** send a Wompi reference. Payments Core generates it and returns it:

```json
{
  "transaction_id": "...",
  "reference": "pay_...",
  "provider": "wompi",
  "status": "pending",
  "checkout": {},
  "payment_init": {}
}
```

The same Core-generated reference is sent to Wompi and stored in `transactions.reference`.

## 4. Direct API flow

For `flow="api"`, the frontend may use the Wompi public information returned in `payment_init` to tokenize a card directly with Wompi. The card number must not be sent to Payments Core.

Then call:

```http
POST /v1/payments/intents/{reference}/charge
Authorization: Bearer <integration-api-key>
```

The final state is not inferred from the browser response. The provider webhook is the source of truth.

## 5. Provider webhook

Wompi is configured with one central URL:

```http
POST /v1/webhooks/wompi
```

Wompi includes the payment reference in the event. Payments Core finds the transaction directly by `reference`.

The transaction already contains:

- `merchant_id`
- `integration_id`
- `provider_slug`

Therefore the Core does not need an Integration slug in the Wompi URL and does not need to guess which Merchant owns the transaction.

After verifying the Wompi signature with the Merchant's credential, the Core updates the transaction and sends a normalized event to the Integration's `webhook_url`.

## 6. Application webhook

The application webhook is provider-agnostic and contains:

```json
{
  "event": "payment.approved",
  "integration": "nexolu-pos",
  "transaction_id": "...",
  "reference": "pay_...",
  "provider": "wompi",
  "provider_transaction_id": "...",
  "amount_cop": 50000,
  "currency": "COP",
  "fee_cop": 2023,
  "net_amount_cop": 47977,
  "status": "approved",
  "metadata": {
    "order_id": "12345"
  }
}
```

The callback is signed with the Integration's webhook secret.

## 7. Transaction lookup

```http
GET /v1/payments/transactions/{reference}
Authorization: Bearer <integration-api-key>
```

The application can poll this endpoint while waiting for the provider webhook.

## 8. Provider abstraction

Wompi is the first `PaymentProvider`. The Core architecture must not require application changes when adding another provider such as Bold.

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
