import json

from django.db import IntegrityError, transaction

from .gateway import StripeGateway
from .models import Payment


class InvalidPayment(ValueError):
    pass


class PaymentNotFound(LookupError):
    pass


def create_payment(amount, currency, idempotency_key, gateway=None):
    _validate_payment(amount, currency, idempotency_key)

    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                amount=amount,
                currency=currency,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        payment = Payment.objects.get(idempotency_key=idempotency_key)
        if payment.amount != amount or payment.currency != currency:
            raise InvalidPayment(
                "idempotency key was already used with different payment details"
            )
        return payment

    gateway = gateway or StripeGateway()
    intent_id = gateway.create_payment_intent(amount, currency, idempotency_key)
    Payment.objects.filter(pk=payment.pk).update(
        stripe_payment_intent_id=intent_id
    )
    payment.stripe_payment_intent_id = intent_id
    return payment


def get_payment(payment_id):
    try:
        return Payment.objects.get(pk=payment_id)
    except Payment.DoesNotExist as exc:
        raise PaymentNotFound from exc


def parse_payment_body(body):
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidPayment("request body must be valid JSON") from exc

    if not isinstance(data, dict):
        raise InvalidPayment("request body must be a JSON object")
    return data


def payment_details(data):
    amount = data.get("amount")
    currency = data.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidPayment("amount must be a positive integer")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isalpha()
    ):
        raise InvalidPayment("currency must be a three-letter code")
    return amount, currency.lower()


def _validate_payment(amount, currency, idempotency_key):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InvalidPayment("Idempotency-Key header is required")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidPayment("amount must be a positive integer")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isalpha()
    ):
        raise InvalidPayment("currency must be a three-letter code")
