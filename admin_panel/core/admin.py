from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import ReviewReport


@admin.register(ReviewReport)
class ReviewReportAdmin(ModelAdmin):
    # ... ваши настройки ...
    list_display = ('id', 'project_name', 'file_path', 'status', 'created_at')
    list_filter = ('status', 'created_at', 'content_type')
    search_fields = ('project_name', 'review_result')
    readonly_fields = ('id', 'created_at', 'file_path')

    fieldsets = (
        (None, {"fields": ('project_name', 'status', 'content_type')}),
        ("Анализируемый файл", {"fields": ('file_path',)}),
        ("Результат анализа AI", {"fields": ('summary', 'review_result'), "classes": ('collapse',)}),
        ("Meta информация", {"fields": ('id', 'created_at'), "classes": ('collapse',)}),
    )

    # --- ПОДКЛЮЧАЕМ СКРИПТ ТЕМЫ ---
    class Media:
        js = ('admin/theme.js',)