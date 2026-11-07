import json
import threading

import stripe
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import override_settings

from .models import LedgerEntry, Payment, WebhookEvent
from .services import check_payment_balance, create_payment

WEBHOOK_SECRET = "whsec_test_secret"


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def create_payment_intent(self, amount, currency, idempotency_key):
        with self.lock:
            self.calls += 1
        return "pi_test"


class FailingOnceGateway(FakeGateway):
    def create_payment_intent(self, amount, currency, idempotency_key):
        with self.lock:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary Stripe failure")
        return "pi_test"


def event_body(event_id, intent_id, event_type="payment_intent.succeeded"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "data": {
                "object": {
                    "id": intent_id,
                    "object": "payment_intent",
                }
            },
        },
        separators=(",", ":"),
    ).encode()


def signature_for(body):
    return stripe.WebhookSignature.generate_signature_header(
        body.decode(), WEBHOOK_SECRET
    )


@override_settings(STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET)
class PaymentTests(TestCase):
    def test_gateway_failure_rolls_back_payment_and_retry_succeeds(self):
        gateway = FailingOnceGateway()

        with self.assertRaises(RuntimeError):
            create_payment(1000, "usd", "order-retry", gateway)
        self.assertFalse(Payment.objects.filter(idempotency_key="order-retry").exists())

        payment = create_payment(1000, "usd", "order-retry", gateway)

        self.assertEqual(payment.stripe_payment_intent_id, "pi_test")
        self.assertEqual(gateway.calls, 2)
        self.assertEqual(Payment.objects.count(), 1)

    def test_same_idempotency_key_uses_one_gateway_call_and_one_payment(self):
        gateway = FakeGateway()

        first = create_payment(1000, "usd", "order-1", gateway)
        second = create_payment(1000, "usd", "order-1", gateway)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_same_key_with_different_amount_returns_422(self):
        Payment.objects.create(
            amount=1000,
            currency="usd",
            idempotency_key="order-1",
        )

        response = self.client.post(
            "/payments",
            data={"amount": 2000, "currency": "usd"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="order-1",
        )

        self.assertEqual(response.status_code, 422)

    def test_bad_webhook_signature_returns_400_and_writes_nothing(self):
        payment = Payment.objects.create(
            amount=1000,
            currency="usd",
            idempotency_key="order-1",
            stripe_payment_intent_id="pi_test",
        )
        body = event_body("evt_bad", payment.stripe_payment_intent_id)
        valid_signature = signature_for(body)
        bad_signature = valid_signature[:-1] + (
            "0" if valid_signature[-1] != "0" else "1"
        )

        response = self.client.post(
            "/webhooks/stripe",
            body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=bad_signature,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)
        self.assertEqual(LedgerEntry.objects.count(), 0)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_duplicate_webhook_creates_one_set_of_ledger_entries(self):
        payment = Payment.objects.create(
            amount=1000,
            currency="usd",
            idempotency_key="order-1",
            stripe_payment_intent_id="pi_test",
        )
        body = event_body("evt_1", payment.stripe_payment_intent_id)
        signature = signature_for(body)

        first = self.client.post(
            "/webhooks/stripe",
            body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature,
        )
        second = self.client.post(
            "/webhooks/stripe",
            body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_late_failed_webhook_does_not_change_succeeded_payment(self):
        payment = Payment.objects.create(
            amount=1000,
            currency="usd",
            idempotency_key="order-1",
            stripe_payment_intent_id="pi_test",
        )
        succeeded_body = event_body(
            "evt_succeeded", payment.stripe_payment_intent_id
        )
        failed_body = event_body(
            "evt_failed",
            payment.stripe_payment_intent_id,
            "payment_intent.payment_failed",
        )

        self.client.post(
            "/webhooks/stripe",
            succeeded_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature_for(succeeded_body),
        )
        response = self.client.post(
            "/webhooks/stripe",
            failed_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature_for(failed_body),
        )

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_succeeded_events_with_different_ids_create_one_ledger_set(self):
        payment = Payment.objects.create(
            amount=1000,
            currency="usd",
            idempotency_key="order-1",
            stripe_payment_intent_id="pi_test",
        )
        first_body = event_body("evt_first", payment.stripe_payment_intent_id)
        second_body = event_body("evt_second", payment.stripe_payment_intent_id)

        first = self.client.post(
            "/webhooks/stripe",
            first_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature_for(first_body),
        )
        second = self.client.post(
            "/webhooks/stripe",
            second_body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature_for(second_body),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(WebhookEvent.objects.count(), 2)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_unknown_payment_intent_returns_500_without_recording_event(self):
        body = event_body("evt_unknown", "pi_unknown")

        response = self.client.post(
            "/webhooks/stripe",
            body,
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE=signature_for(body),
        )

        self.assertEqual(response.status_code, 500)
        self.assertFalse(
            WebhookEvent.objects.filter(stripe_event_id="evt_unknown").exists()
        )

    def test_every_payment_has_equal_debits_and_credits(self):
        payments = [
            Payment.objects.create(
                amount=amount,
                currency="usd",
                idempotency_key=f"order-{amount}",
                stripe_payment_intent_id=f"pi-{amount}",
            )
            for amount in (1000, 2500)
        ]

        for payment in payments:
            LedgerEntry.objects.create(
                payment=payment,
                account="stripe_clearing",
                direction=LedgerEntry.Direction.DEBIT,
                amount=payment.amount,
            )
            LedgerEntry.objects.create(
                payment=payment,
                account="revenue",
                direction=LedgerEntry.Direction.CREDIT,
                amount=payment.amount,
            )

        for payment in Payment.objects.all():
            check_payment_balance(payment)


class PaymentConcurrencyTests(TransactionTestCase):
    def test_twenty_concurrent_requests_create_one_payment_and_one_intent(self):
        gateway = FakeGateway()
        errors = []

        def create():
            try:
                create_payment(1000, "usd", "order-concurrent", gateway)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=create) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(gateway.calls, 1)
