"""Administration and staff-only report history."""

from django.contrib import admin
from django.db import DatabaseError, connection
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView


def health(request: HttpRequest) -> JsonResponse:
    """Expose DB availability without authentication or database details."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        response = JsonResponse({"status": "unavailable"}, status=503)
    else:
        response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [
    path("healthz", health, name="health"),
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
    path("admin/", admin.site.urls),
    path("stats/", include("core.urls")),
]
