"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


API_URL = os.getenv("API_URL", "http://localhost:8457/api")
VIDEO_URL = os.getenv("VIDEO_URL", "http://localhost:8457/video/")
API_TOKEN = os.getenv("API_TOKEN", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
ALLOWED_IPS = _split_csv(os.getenv("ALLOWED_IPS", "127.0.0.1"))

SOURCE_DIR = Path(os.getenv("SOURCE_DIR", "/app/source"))
TARGET_DIR = Path(os.getenv("TARGET_DIR", "/app/target"))
HIDDEN_DIR = Path(os.getenv("HIDDEN_DIR", "/app/hidden"))
IMPORT_DIR = Path(os.getenv("IMPORT_DIR", "/app/import"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
# Path at which media clients (Plex, Jellyfin, the host itself) see the
# archive. Symlinks are written against this path. Defaults to the in-container
# source path so a fresh install produces links that at least resolve inside
# the container; set it to the host-side path for real deployments.
HOST_SOURCE_BASE = Path(os.getenv("HOST_SOURCE_BASE") or str(SOURCE_DIR))

# Number of reverse proxies in front of the app. When > 0, X-Forwarded-For is
# trusted for that many hops so ALLOWED_IPS and login logging see the real
# client address instead of the proxy's.
TRUSTED_PROXY_COUNT = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))

DB_PATH = DATA_DIR / "videos.db"

FLASK_SECRET_KEY_ENV = os.getenv("FLASK_SECRET_KEY")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_LIFETIME_SECONDS = int(os.getenv("SESSION_LIFETIME_SECONDS", str(8 * 3600)))

# Pagination caps applied to API endpoints to prevent unbounded queries.
MAX_PER_PAGE = int(os.getenv("MAX_PER_PAGE", "500"))

HEADERS = {"Authorization": f"Token {API_TOKEN}"} if API_TOKEN else {}

# Static folder shipped by the Astro build — resolved relative to repo root,
# not the cwd, so the entry script can be invoked from anywhere.
_PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PACKAGE_DIR.parent
STATIC_FOLDER = str(REPO_ROOT / "ui" / "dist")
