import uuid

from django.db import models


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=255, unique=True)
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=20, choices=Status, default=Status.PENDING)
    stripe_payment_intent_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)


class WebhookEvent(models.Model):
    stripe_event_id = models.CharField(max_length=255, primary_key=True)
    type = models.CharField(max_length=255)
    processed_at = models.DateTimeField(auto_now_add=True)


class LedgerEntry(models.Model):
    class Direction(models.TextChoices):
        DEBIT = "debit"
        CREDIT = "credit"

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE)
    account = models.CharField(max_length=255)
    direction = models.CharField(max_length=10, choices=Direction)
    amount = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="ledger_entry_amount_positive",
            )
        ]
