from pathlib import Path
import os

# .env loading
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=""):
    v = os.getenv(key)
    return v if v not in (None, "") else default


def env_bool(key, default=False):
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


def env_list(key, default=()):
    raw = env(key, "")
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# Core
SECRET_KEY = env("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["*"])

# Apps
INSTALLED_APPS = [
    "daphne",
    "channels",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "recovery_agent",
]

# Middleware
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Auth validators
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# I18n
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_L10N = False
USE_TZ = True
DATETIME_FORMAT = "N j, Y, g:i:s A"
DATE_FORMAT = "N j, Y"
TIME_FORMAT = "g:i:s A"

# Static / media
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# CSRF / sessions
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS",
    ["http://127.0.0.1:8000", "http://localhost:8000"],
)

# CORS
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
CORS_ALLOW_METHODS = ["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"]
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://127.0.0.1:8000"],
)

# DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# Channels
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": env("CHANNEL_BACKEND", "channels.layers.InMemoryChannelLayer"),
    }
}

# Telephony / proxy
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", True)

# ──────────────────────────────────────────────────────────────
# API keys — all loaded from .env, never hard-coded
# ──────────────────────────────────────────────────────────────

# Bharat Router
BHARATROUTER_API_KEY = env("BHARATROUTER_API_KEY")
BHARATROUTER_BASE_URL = env("BHARATROUTER_BASE_URL", "https://api.bharatrouter.com/v1")
BHARATROUTER_MODEL = env("BHARATROUTER_MODEL", "gemma-4-31b-it")
BHARATROUTER_DATA_POLICY = env("BHARATROUTER_DATA_POLICY", "india_only")

# Krutrim
KRUTRIM_API_KEY = env("KRUTRIM_API_KEY")
KRUTRIM_BASE_URL = env("KRUTRIM_BASE_URL", "https://cloud.olakrutrim.com/v1")
KRUTRIM_MODEL = env("KRUTRIM_MODEL", "gemma-4-31b-it")
KRUTRIM_TEMPERATURE = float(env("KRUTRIM_TEMPERATURE", "0.4"))

# Google Gemini
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_CACHE_TTL_SECONDS = int(env("GEMINI_CACHE_TTL_SECONDS", "86400"))

# OpenAI / Sarvam
OPENAI_API_KEY = env("OPENAI_API_KEY")
SARVAM_API_KEY = env("SARVAM_API_KEY")

# STT
STT_API_KEY = env("STT_API_KEY")
STT_API_URL = env("STT_API_URL", "https://api.60dB.ai/v1/stt")
SIXTY_DB_API_KEY = env("SIXTY_DB_API_KEY")
SIXTY_DB_BASE_URL = env("SIXTY_DB_BASE_URL", "https://api.60db.ai")
SIXTY_DB_STT_LANGUAGES = env("SIXTY_DB_STT_LANGUAGES", "hi,en")
GNANI_API_KEY = env("GNANI_API_KEY")

# TTS / Murf
MURF_API_KEY = env("MURF_API_KEY")
TTS_API_KEY = env("TTS_API_KEY")
TTS_API_URL = env("TTS_API_URL", "https://api.murf.ai/v1/speech/generate")

# Telephony
EXOTEL_API_KEY = env("EXOTEL_API_KEY")
EXOTEL_API_TOKEN = env("EXOTEL_API_TOKEN")
EXOTEL_ACCOUNT_SID = env("EXOTEL_ACCOUNT_SID", "triosoft1")
PLIVO_AUTH_ID = env("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = env("PLIVO_AUTH_TOKEN")
PLIVO_FROM_NUMBER = env("PLIVO_FROM_NUMBER", "+918031829789")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Infra
REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")
OLLAMA_URL = env("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "llama3")
CHROMA_PATH = env("CHROMA_PATH", "./chroma_data")
COLLECTION_NAME = env("COLLECTION_NAME", "honda_knowledge")
HONDA_DATA_PATH = env("HONDA_DATA_PATH", str(BASE_DIR / "honda_data"))

# Freeswitch (dev)
FREESWITCH_ESL_PASSWORD = env("FREESWITCH_ESL_PASSWORD", "Mission@2026")

# Storage paths
CALL_RECORDINGS_DIR = MEDIA_ROOT / "call_recordings"
SIP_TTS_AUDIO_DIR = MEDIA_ROOT / "tts_audio"
for _d in (CALL_RECORDINGS_DIR, SIP_TTS_AUDIO_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Call settings
CALL_TIMEOUT = 30
MAX_CALL_DURATION = 600

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "voice_bot": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "recovery_agent": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}