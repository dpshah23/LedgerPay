from django.urls import path

from ledger import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("payments", views.payments, name="payments"),
    path("payments/<uuid:payment_id>", views.payment_detail, name="payment_detail"),
]
