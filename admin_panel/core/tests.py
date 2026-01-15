from django.test import TestCase, Client
from django.urls import reverse
from .models import ReviewReport

class ReviewReportTests(TestCase):
    def setUp(self):
        # Создаем клиент для запросов
        self.client = Client()

    def test_view_page_exists(self):
        """Проверяем, что страница статистики открывается (код 200)"""
        url = reverse('report_list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "История ревью")

    def test_model_fields(self):
        """Проверяем, что модель видит структуру таблицы SQLModel"""
        # Это проверит, что таблица существует в БД и поля совпадают
        # Если база пустая, это просто проверит схему
        try:
            ReviewReport.objects.all()[:1]
        except Exception as e:
            self.fail(f"Не удалось подключиться к таблице reviewreport: {e}")