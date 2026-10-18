"""Inspect API reports without allowing Django to mutate their lifecycle."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.http import HttpRequest

from .models import ReviewReport

if TYPE_CHECKING:
    # Unfold implements Django's ModelAdmin API but does not publish type stubs.
    from django.contrib.admin import ModelAdmin

    ReportAdminBase = ModelAdmin[ReviewReport]
else:
    from unfold.admin import ModelAdmin as ReportAdminBase


@admin.register(ReviewReport)
class ReviewReportAdmin(ReportAdminBase):
    list_display = ("id", "project_name", "file_path", "status", "created_at")
    list_filter = ("status", "created_at", "content_type")
    search_fields = ("project_name", "file_path")
    ordering = ("-created_at", "-id")
    readonly_fields = (
        "id",
        "project_name",
        "file_path",
        "content_type",
        "summary",
        "review_result",
        "status",
        "created_at",
        "owner_id",
        "job_id",
    )
    actions = None
    list_per_page = 20

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: ReviewReport | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: ReviewReport | None = None) -> bool:
        return False
