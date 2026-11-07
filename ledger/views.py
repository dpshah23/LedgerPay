import logging

import stripe
from django.conf import settings
from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .services import (
    InvalidPayment,
    PaymentNotFound,
    WebhookPaymentNotFound,
    create_payment,
    get_payment,
    parse_payment_body,
    payment_details,
    process_webhook,
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


@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    signature = request.headers.get("Stripe-Signature")
    if not signature:
        return JsonResponse({"error": "missing Stripe-Signature"}, status=400)

    try:
        event = stripe.Webhook.construct_event(
            request.body,
            signature,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.SignatureVerificationError):
        return JsonResponse({"error": "invalid webhook signature"}, status=400)

    try:
        process_webhook(event)
    except WebhookPaymentNotFound:
        logger.error("webhook references unknown payment intent")
        return JsonResponse({"error": "unknown payment intent"}, status=500)
    except DatabaseError:
        logger.exception("processing Stripe webhook")
        return JsonResponse({"error": "webhook processing failed"}, status=500)

    return JsonResponse({"status": "ok"})
