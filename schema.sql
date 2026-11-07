CREATE TABLE payments (
    id uuid PRIMARY KEY,
    idempotency_key text UNIQUE NOT NULL,
    amount bigint NOT NULL,
    currency text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    stripe_payment_intent_id text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_events (
    stripe_event_id text PRIMARY KEY,
    type text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (
    id bigserial PRIMARY KEY,
    payment_id uuid NOT NULL REFERENCES payments(id),
    account text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount bigint NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
