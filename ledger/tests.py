import json
import os
import threading
import unittest

import stripe
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import override_settings

from .models import LedgerEntry, Payment, WebhookEvent
from .services import check_payment_balance, create_payment

TEST_SECRET = "whsec_test_secret"


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def create_payment_intent(self, amount, currency, idempotency_key):
        with self.lock:
            self.calls += 1
        return "pi_test"


def webhook_body(event_id, intent_id, event_type="payment_intent.succeeded"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "data": {"object": {"id": intent_id, "object": "payment_intent"}},
        },
        separators=(",", ":"),
    ).encode()


if not os.getenv("TEST_DATABASE_URL"):

    class DatabaseTestsSkipped(SimpleTestCase):
        @staticmethod
        def test_set_test_database_url():
            raise unittest.SkipTest("TEST_DATABASE_URL is not set")

else:

    @override_settings(STRIPE_WEBHOOK_SECRET=TEST_SECRET)
    class PaymentTests(TestCase):
        def test_same_idempotency_key_uses_one_gateway_call(self):
            gateway = FakeGateway()

            first = create_payment(1000, "usd", "order-1", gateway)
            second = create_payment(1000, "usd", "order-1", gateway)

            self.assertEqual(first.pk, second.pk)
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(Payment.objects.count(), 1)

        def test_same_key_with_different_details_is_invalid(self):
            gateway = FakeGateway()
            create_payment(1000, "usd", "order-1", gateway)

            with self.assertRaises(ValueError):
                create_payment(2000, "usd", "order-1", gateway)

        def test_bad_webhook_signature_writes_nothing(self):
            payment = Payment.objects.create(
                amount=1000,
                currency="usd",
                idempotency_key="order-1",
                stripe_payment_intent_id="pi_test",
            )
            body = webhook_body("evt_bad", payment.stripe_payment_intent_id)

            response = self.client.post(
                "/webhooks/stripe",
                body,
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=bad",
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(WebhookEvent.objects.count(), 0)
            self.assertEqual(LedgerEntry.objects.count(), 0)
            payment.refresh_from_db()
            self.assertEqual(payment.status, Payment.Status.PENDING)

        def test_duplicate_webhook_creates_one_set_of_entries(self):
            payment = Payment.objects.create(
                amount=1000,
                currency="usd",
                idempotency_key="order-1",
                stripe_payment_intent_id="pi_test",
            )
            body = webhook_body("evt_1", payment.stripe_payment_intent_id)
            signature = stripe.WebhookSignature.generate_signature_header(
                body.decode(), TEST_SECRET
            )

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

        def test_unknown_payment_intent_rolls_back_event(self):
            body = webhook_body("evt_unknown", "pi_unknown")
            signature = stripe.WebhookSignature.generate_signature_header(
                body.decode(), TEST_SECRET
            )

            response = self.client.post(
                "/webhooks/stripe",
                body,
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=signature,
            )

            self.assertEqual(response.status_code, 500)
            self.assertFalse(
                WebhookEvent.objects.filter(stripe_event_id="evt_unknown").exists()
            )

        def test_payment_ledger_debits_equal_credits(self):
            payment = Payment.objects.create(
                amount=1000,
                currency="usd",
                idempotency_key="order-1",
                stripe_payment_intent_id="pi_test",
            )
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

            check_payment_balance(payment)


    class PaymentConcurrencyTests(TransactionTestCase):
        reset_sequences = True

        def test_concurrent_same_key_creates_one_payment_and_one_intent(self):
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
