"""
Django settings for Queueless project.
Development and production configuration.
"""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured


# ============================================================
# BASE DIRECTORY
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Load local .env file if available.
# Render environment variables will also be available through os.environ.
load_dotenv(BASE_DIR / ".env", override=False)


# ============================================================
# SECURITY
# ============================================================

DEBUG = os.environ.get("DJANGO_DEBUG", "True").strip().lower() in {
    "1", "true", "yes", "on"
}

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = secrets.token_urlsafe(50)
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be configured in production."
        )


# Allowed hosts
_allowed_hosts_env = os.environ.get("DJANGO_ALLOWED_HOSTS", "")

ALLOWED_HOSTS = [
    host.strip()
    for host in _allowed_hosts_env.split(",")
    if host.strip()
]

if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
elif not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must be configured when DEBUG is False."
    )


# CSRF trusted origins
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS", ""
    ).split(",")
    if origin.strip()
]


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "rest_framework",

    "corsheaders",

    "accounts",
    "user",
]


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    # Static file serving in production
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",

    # CORS middleware
    "corsheaders.middleware.CorsMiddleware",

    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ============================================================
# CORS
# ============================================================

# Configure only if your project has separate frontend origins.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

CORS_ALLOW_CREDENTIALS = True


# ============================================================
# URL CONFIGURATION
# ============================================================

ROOT_URLCONF = "Queueless.urls"

WSGI_APPLICATION = "Queueless.wsgi.application"


# ============================================================
# TEMPLATES
# ============================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",

        "DIRS": [
            BASE_DIR / "template",
        ],

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


# ============================================================
# DATABASE - MYSQL
# ============================================================

_database_env_keys = (
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
)

_missing_database_keys = [
    key for key in _database_env_keys if not os.environ.get(key)
]

if _missing_database_keys:
    raise ImproperlyConfigured(
        "Missing database environment variables: "
        + ", ".join(_missing_database_keys)
    )


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
        "PORT": os.environ["DB_PORT"],

        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": (
                "SET sql_mode='STRICT_TRANS_TABLES', "
                "default_storage_engine=INNODB"
            ),
        },

        "CONN_MAX_AGE": 60,
    }
}


# ============================================================
# PASSWORD VALIDATION
# ============================================================

AUTH_PASSWORD_VALIDATORS = []


# ============================================================
# INTERNATIONALIZATION
# ============================================================

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Asia/Kathmandu"

USE_I18N = True

USE_TZ = True


# ============================================================
# QUEUELESS APPLICATION SETTINGS
# ============================================================

CUSTOMER_WAIT_ESTIMATE_MINUTES = 10


# ============================================================
# STATIC FILES
# ============================================================

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"


STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },

    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}


# ============================================================
# MEDIA FILES
# ============================================================

MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media"


# ============================================================
# EMAIL / OTP CONFIGURATION
# ============================================================

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = os.environ.get(
    "EMAIL_HOST", "smtp.gmail.com"
)

EMAIL_PORT = int(
    os.environ.get("EMAIL_PORT", "587")
)

EMAIL_USE_TLS = (
    os.environ.get("EMAIL_USE_TLS", "True").strip().lower()
    in {"1", "true", "yes", "on"}
)

EMAIL_USE_SSL = (
    os.environ.get("EMAIL_USE_SSL", "False").strip().lower()
    in {"1", "true", "yes", "on"}
)

if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured(
        "EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled."
    )

EMAIL_TIMEOUT = 10

EMAIL_HOST_USER = os.environ.get(
    "EMAIL_HOST_USER", ""
)

EMAIL_HOST_PASSWORD = os.environ.get(
    "EMAIL_HOST_PASSWORD", ""
)

DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", EMAIL_HOST_USER
)


# ============================================================
# WEB PUSH CONFIGURATION
# ============================================================

WEB_PUSH_VAPID_PUBLIC_KEY = os.environ.get(
    "WEB_PUSH_VAPID_PUBLIC_KEY", ""
).strip()

WEB_PUSH_VAPID_PRIVATE_KEY = os.environ.get(
    "WEB_PUSH_VAPID_PRIVATE_KEY", ""
).strip()

WEB_PUSH_VAPID_SUBJECT = os.environ.get(
    "WEB_PUSH_VAPID_SUBJECT",
    f"mailto:{EMAIL_HOST_USER}" if EMAIL_HOST_USER else "",
).strip()

WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY = os.environ.get(
    "WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY", ""
).strip()


# ============================================================
# ADMIN CONFIGURATION
# ============================================================

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


# ============================================================
# PRODUCTION SECURITY
# ============================================================

if not DEBUG:

    # Render terminates HTTPS at its proxy.
    SECURE_PROXY_SSL_HEADER = (
        "HTTP_X_FORWARDED_PROTO",
        "https",
    )

    SECURE_SSL_REDIRECT = True

    SESSION_COOKIE_SECURE = True

    CSRF_COOKIE_SECURE = True

    SECURE_CONTENT_TYPE_NOSNIFF = True

    SECURE_BROWSER_XSS_FILTER = True

    X_FRAME_OPTIONS = "DENY"

    SECURE_REFERRER_POLICY = "same-origin"

    # HSTS: enable after confirming HTTPS works correctly.
    SECURE_HSTS_SECONDS = int(
        os.environ.get("SECURE_HSTS_SECONDS", "0")
    )

    SECURE_HSTS_INCLUDE_SUBDOMAINS = False

    SECURE_HSTS_PRELOAD = False


# ============================================================
# DEFAULT PRIMARY KEY
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ============================================================
# EMAIL CONFIGURATION WARNING
# ============================================================

if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
    print(
        "\nWARNING: Gmail email credentials are missing. "
        "OTP email delivery will not work until configured.\n"
    )