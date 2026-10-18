"""Staff-only report history; pagination bounds retained data per request."""

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.views.generic import DetailView, ListView

from .models import ReviewReport

if TYPE_CHECKING:
    ReportListBase = ListView[ReviewReport]
    ReportDetailBase = DetailView[ReviewReport]
else:
    ReportListBase = ListView
    ReportDetailBase = DetailView


class StaffReportAccessMixin(LoginRequiredMixin, PermissionRequiredMixin):
    request: HttpRequest
    permission_required = "core.view_reviewreport"

    def has_permission(self) -> bool:
        # SQLModel account ownership and Django administration are distinct:
        # this cross-user view is available to explicitly authorized staff only.
        return self.request.user.is_staff and super().has_permission()

    def dispatch(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponseBase:
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response


class ReviewReportListView(StaffReportAccessMixin, ReportListBase):
    model = ReviewReport
    template_name = "core/reports_list.html"
    context_object_name = "reports"
    paginate_by = 20

    def get_queryset(self) -> QuerySet[ReviewReport]:
        return ReviewReport.objects.defer("review_result").order_by("-created_at", "-id")


class ReviewReportDetailView(StaffReportAccessMixin, ReportDetailBase):
    model = ReviewReport
    template_name = "core/report_detail.html"
    context_object_name = "report"
