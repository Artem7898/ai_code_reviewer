"""
URL configuration for admin_panel project.
"""
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect

urlpatterns = [
    # Перенаправляем главную страницу сразу в админку
    path('', lambda request: redirect('admin/'), name='root'),

    # Подключаем Unfold URL вместо стандартных admin.site.urls
    path('admin/', admin.site.urls),

    # Ваши собственные URL
    path('stats/', include('core.urls')),
]