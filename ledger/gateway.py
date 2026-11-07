import stripe
from django.conf import settings


class StripeGateway:
    def __init__(self, secret_key=None):
        self.client = stripe.StripeClient(secret_key or settings.STRIPE_SECRET_KEY)

    def create_payment_intent(self, amount, currency, idempotency_key):
        intent = self.client.v1.payment_intents.create(
            {"amount": amount, "currency": currency},
            {"idempotency_key": idempotency_key},
        )
        return intent.id
