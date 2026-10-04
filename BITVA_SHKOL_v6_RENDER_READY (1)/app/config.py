from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load project-local .env for Windows/local runs; real process environment wins.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


APP_ENV = env("APP_ENV", "development")
DATABASE_URL = env("DATABASE_URL", "sqlite:///./data/leaderboard.db")
SECRET_KEY = env("SECRET_KEY", "change-me-in-production")
ADMIN_USERNAME = env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "change-me-in-production")
ADMIN_LOGIN_PATH = env("ADMIN_LOGIN_PATH", "/ljungberg-access")
DONATION_URL = env("DONATION_URL", "https://www.donationalerts.com/")
TURNSTILE_SITE_KEY = env("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
TURNSTILE_SECRET_KEY = env("TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")
TURNSTILE_ENABLED = env("TURNSTILE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
ADMIN_TURNSTILE_ENABLED = env("ADMIN_TURNSTILE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
TRUST_PROXY = env("TRUST_PROXY", "false").lower() in {"1", "true", "yes", "on"}
REDIS_URL = env("REDIS_URL", "")
MEDIA_BACKEND = env("MEDIA_BACKEND", "db" if DATABASE_URL.startswith(("postgresql", "postgres://")) else "filesystem")
MEDIA_ROOT = env("MEDIA_ROOT", "./media")
RATE_LIMIT_SAME_POINT = int(env("RATE_LIMIT_SAME_POINT", "800"))
RATE_LIMIT_IP_TOTAL = int(env("RATE_LIMIT_IP_TOTAL", "1500"))
RATE_LIMIT_SESSION_TOTAL = int(env("RATE_LIMIT_SESSION_TOTAL", "1500"))
RATE_LIMIT_SESSION_SAME_POINT = int(env("RATE_LIMIT_SESSION_SAME_POINT", "800"))
MAX_BATCH_CLICKS = int(env("MAX_BATCH_CLICKS", "60"))
PUBLIC_UPDATE_SECONDS = float(env("PUBLIC_UPDATE_SECONDS", "5"))
SESSION_MINUTES = int(env("SESSION_MINUTES", "30"))
MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "10"))
MAX_EVENT_AUDIO_MB = int(env("MAX_EVENT_AUDIO_MB", "25"))
APP_NAME = env("APP_NAME", "БИТВА ШКОЛ")
PUBLIC_COUNTRIES = {x.strip().upper() for x in env("PUBLIC_COUNTRIES", "RU").split(",") if x.strip()}
VISIT_COOKIE_DAYS = int(env("VISIT_COOKIE_DAYS", "365"))
VISIT_RATE_LIMIT_PER_IP = int(env("VISIT_RATE_LIMIT_PER_IP", "10"))
VISIT_RATE_WINDOW_SECONDS = int(env("VISIT_RATE_WINDOW_SECONDS", "60"))
ONLINE_TTL_SECONDS = int(env("ONLINE_TTL_SECONDS", "25"))

TYPE_LABELS_RU = {
    "general": "Общеобразовательное",
    "boarding": "Школа-интернат",
    "private": "Частная школа",
    "college": "Колледж / техникум",
    "university": "ВУЗ",
    "other": "Учебное учреждение",
}
TYPE_LABELS_EN = {
    "general": "General school",
    "boarding": "Boarding school",
    "private": "Private school",
    "college": "College / technical school",
    "university": "University",
    "other": "Educational institution",
}
