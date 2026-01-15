from django.views.generic import ListView
from .models import ReviewReport


class ReviewReportListView(ListView):
    """
    Отображает список всех отчетов ревью.
    Таблица управляется FastAPI, мы только читаем.
    """
    model = ReviewReport
    template_name = 'core/reports_list.html'
    context_object_name = 'reports'
    paginate_by = 20  # Пагинация по 20 штук на странице

    def get_queryset(self):
        # Сортируем от новых к старым
        return ReviewReport.objects.all().order_by('-created_at')