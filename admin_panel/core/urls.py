from django.urls import path
from .views import ReviewReportListView

urlpatterns = [
    path('', ReviewReportListView.as_view(), name='report_list'),
]