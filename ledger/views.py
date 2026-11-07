import logging

import stripe
from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse

from .services import (
    InvalidPayment,
    PaymentNotFound,
    create_payment,
    get_payment,
    parse_payment_body,
    payment_details,
)

logger = logging.getLogger(__name__)


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"error": "database unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def payments(request):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    try:
        amount, currency = payment_details(parse_payment_body(request.body))
        payment = create_payment(
            amount,
            currency,
            request.headers.get("Idempotency-Key"),
        )
    except InvalidPayment as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    except stripe.StripeError:
        logger.exception("creating Stripe payment intent")
        return JsonResponse({"error": "payment creation failed"}, status=502)
    except DatabaseError:
        logger.exception("creating payment")
        return JsonResponse({"error": "payment creation failed"}, status=502)

    return JsonResponse(payment_json(payment), status=201)


def payment_detail(request, payment_id):
    if request.method != "GET":
        return JsonResponse({"error": "method not allowed"}, status=405)
    try:
        payment = get_payment(payment_id)
    except PaymentNotFound:
        return JsonResponse({"error": "payment not found"}, status=404)
    return JsonResponse(payment_json(payment))


def payment_json(payment):
    return {
        "id": str(payment.id),
        "idempotency_key": payment.idempotency_key,
        "amount": payment.amount,
        "currency": payment.currency,
        "status": payment.status,
        "stripe_payment_intent_id": payment.stripe_payment_intent_id,
        "created_at": payment.created_at.isoformat(),
    }
