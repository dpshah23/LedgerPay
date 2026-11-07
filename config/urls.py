from django.urls import path

from ledger import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
]
