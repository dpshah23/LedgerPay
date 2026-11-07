import json

from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Sum, When

from .gateway import StripeGateway
from .models import LedgerEntry, Payment, WebhookEvent


class InvalidPayment(ValueError):
    pass


class PaymentNotFound(LookupError):
    pass


class WebhookPaymentNotFound(LookupError):
    pass


class LedgerImbalance(RuntimeError):
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
            gateway = gateway or StripeGateway()
            payment.stripe_payment_intent_id = gateway.create_payment_intent(
                amount, currency, idempotency_key
            )
            payment.save(update_fields=["stripe_payment_intent_id"])
    except IntegrityError:
        payment = Payment.objects.get(idempotency_key=idempotency_key)
        if payment.amount != amount or payment.currency != currency:
            raise InvalidPayment(
                "idempotency key was already used with different payment details"
            )
    return payment


def get_payment(payment_id):
    try:
        return Payment.objects.get(pk=payment_id)
    except Payment.DoesNotExist as exc:
        raise PaymentNotFound from exc


def process_webhook(event):
    event_type = event["type"]
    event_id = event["id"]

    with transaction.atomic():
        try:
            with transaction.atomic():
                WebhookEvent.objects.create(
                    stripe_event_id=event_id,
                    type=event_type,
                )
        except IntegrityError:
            return

        if event_type not in (
            "payment_intent.succeeded",
            "payment_intent.payment_failed",
        ):
            return

        intent = event["data"]["object"]
        try:
            payment = Payment.objects.select_for_update().get(
                stripe_payment_intent_id=intent["id"]
            )
        except Payment.DoesNotExist as exc:
            raise WebhookPaymentNotFound from exc

        if event_type == "payment_intent.succeeded":
            if payment.status == Payment.Status.SUCCEEDED:
                return
            payment.status = Payment.Status.SUCCEEDED
            payment.save(update_fields=["status"])
            LedgerEntry.objects.bulk_create(
                [
                    LedgerEntry(
                        payment=payment,
                        account="stripe_clearing",
                        direction=LedgerEntry.Direction.DEBIT,
                        amount=payment.amount,
                    ),
                    LedgerEntry(
                        payment=payment,
                        account="revenue",
                        direction=LedgerEntry.Direction.CREDIT,
                        amount=payment.amount,
                    ),
                ]
            )
            check_payment_balance(payment)
        elif payment.status != Payment.Status.SUCCEEDED:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status"])


def check_payment_balance(payment):
    totals = _ledger_totals(LedgerEntry.objects.filter(payment=payment))
    debits = totals["debits"] or 0
    credits = totals["credits"] or 0
    if debits != credits:
        raise LedgerImbalance(
            f"payment {payment.pk} has {debits} in debits and {credits} in credits"
        )


def get_ledger(account):
    entries = LedgerEntry.objects.filter(account=account).order_by("created_at", "id")
    totals = _ledger_totals(entries)
    balance = (totals["debits"] or 0) - (totals["credits"] or 0)
    return entries, balance


def _ledger_totals(entries):
    return entries.aggregate(
        debits=Sum(
            Case(
                When(direction=LedgerEntry.Direction.DEBIT, then="amount"),
                default=0,
                output_field=IntegerField(),
            )
        ),
        credits=Sum(
            Case(
                When(direction=LedgerEntry.Direction.CREDIT, then="amount"),
                default=0,
                output_field=IntegerField(),
            )
        ),
    )


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
    _validate_details(amount, currency)
    return amount, currency.lower()


def _validate_payment(amount, currency, idempotency_key):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InvalidPayment("Idempotency-Key header is required")
    _validate_details(amount, currency)


def _validate_details(amount, currency):
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidPayment("amount must be a positive integer")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isalpha()
    ):
        raise InvalidPayment("currency must be a three-letter code")
