from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"error": "database unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
