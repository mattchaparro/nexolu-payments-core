# Nexolú Payments Core — Multi-Merchant Architecture

## Domain model

`Merchant` is the company that owns payment-provider accounts.

`Integration` is an application authorized to consume Payments Core. A Merchant can have multiple Integrations.

`ProviderCredential` belongs to a Merchant, not an Integration. This means POS and Spa can use the same Wompi account when they belong to the same company, while a school can have a completely independent Wompi account.

```text
Merchant A
├── Wompi credentials
├── Integration: POS
└── Integration: Spa

Merchant B
├── Wompi credentials
└── Integration: Colegio
```

## Integration credentials

When an Integration is provisioned, Payments Core generates:

- `api_key`: used by the application in `Authorization: Bearer <api_key>`.
- `webhook_secret`: used by Payments Core to sign callbacks to the application.

They are not supplied by the application source code.

The provisioning API is protected by the server-side `PROVISIONING_KEY`. A future management frontend must not expose that key to the browser; it should call the provisioning API from an authenticated backend.

## Provider credentials

Wompi credentials are configured against the Merchant:

```http
POST /v1/admin/merchants/{merchant_id}/providers/wompi
X-Payments-Provisioning-Key: <provisioning-key>
```

Private Wompi credentials are stored through the existing Fernet `EncryptedString` mechanism. Only the Wompi public key is returned by the configuration-status endpoint.

## Transactions

Payments Core generates the transaction `reference`. The consumer does not supply it.

```text
Application
   ↓
POST /v1/payments/intents
   ↓
Payments Core
   ├── identifies Integration from API key
   ├── gets Merchant from Integration
   ├── gets Merchant's provider credential
   ├── generates reference
   ├── creates Transaction
   └── sends reference to provider
```

`Transaction` stores both `merchant_id` and `integration_id` deliberately. They are a routing/context snapshot, not accidental duplication.

`reference` is globally unique and indexed. It is the primary routing key for provider webhooks.

## Wompi webhook

There is one Wompi endpoint:

```http
POST /v1/webhooks/wompi
```

Wompi sends the transaction reference. Payments Core then:

1. Finds `Transaction` by indexed `reference`.
2. Reads `merchant_id` and `integration_id` directly from the transaction.
3. Loads the Wompi credentials belonging to that Merchant/environment.
4. Verifies the Wompi signature.
5. Parses the provider event into the normalized Core event.
6. Updates the Transaction idempotently.
7. Notifies the Integration associated with the Transaction.

This is why POS and Spa do not require separate Wompi callback URLs when they share the same Merchant Wompi account.

## Application webhook

After a transaction changes state, Payments Core sends a normalized event to `Integration.webhook_url`.

The payload contains Core identifiers and application metadata, not the raw Wompi webhook contract.

The callback is signed with `Integration.webhook_secret` using the existing Nexolú webhook signing mechanism.

## Provider abstraction

Wompi remains an implementation of `PaymentProvider`.

The Core should not contain Wompi-specific branching in the orchestration layer. A future provider such as Bold or another payment provider should provide its own implementation and credential mapping.

```text
Application
    ↓
Payments Core
    ↓
PaymentProvider
    ↓
Wompi / Bold / future provider
```

## Provisioning flow

```text
Create Merchant
      ↓
Create Integration
      ↓
Core generates API key + webhook secret
      ↓
Configure provider credentials for Merchant
      ↓
Application receives its API key once
      ↓
Application creates payment intents
```

The provisioning endpoints are an initial backend/admin contract. They are not intended to be called directly by an untrusted browser.
