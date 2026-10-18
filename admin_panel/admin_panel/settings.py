"""Administration settings shared with the API's validated configuration."""

from pathlib import Path

from django.urls import reverse_lazy

from app.settings import get_settings

config = get_settings()
BASE_DIR = Path(__file__).resolve().parent.parent
if config.secret_key is None:
    raise RuntimeError("Validated settings must provide a signing key")
SECRET_KEY = config.secret_key.get_secret_value()
DEBUG = config.django_debug
ALLOWED_HOSTS = config.allowed_hosts

INSTALLED_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "admin_panel.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "admin_panel.wsgi.application"
ASGI_APPLICATION = "admin_panel.asgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": config.database_path,
        "OPTIONS": {"timeout": 20},
    }
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = config.data_dir / "django-static"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "/stats/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = config.environment == "production"
CSRF_COOKIE_SECURE = config.environment == "production"
SECURE_SSL_REDIRECT = config.environment == "production"
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_HSTS_SECONDS = 31_536_000 if config.environment == "production" else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = config.environment == "production"
SECURE_HSTS_PRELOAD = config.environment == "production"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Current Unfold uses separate THEME and COLORS keys and Django URL names.
UNFOLD = {
    "SITE_HEADER": "AI Code Reviewer",
    "SITE_TITLE": "AI Reviewer",
    "SITE_SUBHEADER": "Отчёты и доступ",
    "THEME": "light",
    "SHOW_HISTORY": False,
    "SIDEBAR": {
        "show_search": True,
        "navigation": [
            {
                "title": "Ревью кода",
                "items": [
                    {
                        "title": "Отчёты",
                        "icon": "description",
                        "link": reverse_lazy("admin:core_reviewreport_changelist"),
                    },
                    {
                        "title": "История",
                        "icon": "history",
                        "link": reverse_lazy("report_list"),
                    },
                ],
            },
            {
                "title": "Система",
                "items": [
                    {
                        "title": "Пользователи Django",
                        "icon": "group",
                        "link": reverse_lazy("admin:auth_user_changelist"),
                    }
                ],
            },
        ],
    },
}
