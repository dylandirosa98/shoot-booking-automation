"""
Django settings for videographer-scheduling-automation.

Environment-driven: all secrets / per-env config come from .env (local)
or Railway env vars (production). See .env.example for the full list.
"""
from pathlib import Path
import os
import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# --- Core ---
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
DEBUG = os.getenv("DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Railway sets RAILWAY_PUBLIC_DOMAIN automatically — trust it.
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
if RAILWAY_DOMAIN:
    ALLOWED_HOSTS.append(RAILWAY_DOMAIN)
    CSRF_TRUSTED_ORIGINS = [f"https://{RAILWAY_DOMAIN}"]


# --- Apps ---
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_apscheduler",
    "scheduler",
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

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "config.wsgi.application"


# --- Database ---
# Local: SQLite by default. Production: Postgres via DATABASE_URL on Railway.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# --- Auth ---
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- i18n / time ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True


# --- Static files ---
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- App-specific config (read from env) ---
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
GOOGLE_CALENDAR_OWNER_EMAIL = os.getenv("GOOGLE_CALENDAR_OWNER_EMAIL", "")
# Optional destination folder. Leave blank to create shoot folders in My Drive.
GOOGLE_DRIVE_PARENT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID", "")

PIPEDRIVE_WEBHOOK_SECRET = os.getenv("PIPEDRIVE_WEBHOOK_SECRET", "")
PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN", "")
PIPEDRIVE_COMPANY_DOMAIN = os.getenv("PIPEDRIVE_COMPANY_DOMAIN", "")

CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN", "")

ESCALATION_HOURS = int(os.getenv("ESCALATION_HOURS", "24"))
SCORE_PENALTY_PER_MINUTE = float(os.getenv("SCORE_PENALTY_PER_MINUTE", "0.01"))
MAX_DRIVE_MINUTES = int(os.getenv("MAX_DRIVE_MINUTES", "180"))

NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
