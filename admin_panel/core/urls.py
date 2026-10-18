from django.urls import path

from .views import ReviewReportDetailView, ReviewReportListView

urlpatterns = [
    path("", ReviewReportListView.as_view(), name="report_list"),
    path("<int:pk>/", ReviewReportDetailView.as_view(), name="report_detail"),
]
