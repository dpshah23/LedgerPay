# LedgerPay

LedgerPay is a small Django service that creates Stripe PaymentIntents and records
their results in a double-entry ledger backed by PostgreSQL.

## Architecture

`ledger/views.py` handles HTTP input and JSON responses. Payment creation and
webhook processing live in `ledger/services.py`. Stripe calls are isolated in
`ledger/gateway.py`. Django models provide the database constraints used for
idempotency, webhook deduplication, and ledger validation.

## Run locally

1. Copy `.env.example` to `.env` and add Stripe test-mode keys.
2. Start the database and application:

   ```powershell
   docker compose up -d; python manage.py migrate; python manage.py runserver
   ```

3. In another terminal, forward Stripe test webhooks:

   ```powershell
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```

The local database is exposed on port 5433 because port 5432 may be used by
another PostgreSQL installation. The API health check is available at
`http://127.0.0.1:8000/healthz`.

Create a payment with an idempotency key:

```powershell
$headers = @{ "Idempotency-Key" = "order-001" }
$body = '{"amount":1000,"currency":"usd"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/payments `
  -Headers $headers -ContentType "application/json" -Body $body
```

Payment amounts are integer values in the smallest currency unit.

## Design decisions

- Payment idempotency is enforced by the unique `Payment.idempotency_key`
  constraint. A retry loads the existing payment and does not create another
  Stripe PaymentIntent.
- Webhook deduplication is enforced by the unique `WebhookEvent.stripe_event_id`
  primary key. The event, payment status update, and successful-payment ledger
  entries are committed in one transaction.
- If Stripe succeeds but the server crashes before saving the Stripe
  PaymentIntent ID, the payment remains pending. A later request with the same
  idempotency key returns that stored payment without another Stripe call. This
  implementation does not include a reconciliation job for that failure window.
- Out-of-order webhooks for unknown payment intents roll back the event insert.
  Stripe can then retry the event after the payment row exists.

## Not handled

Refunds, disputes, multi-currency accounting, and authentication are not
implemented.
