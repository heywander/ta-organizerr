"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import math
import os
from pathlib import Path


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def _nonnegative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw_value!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number, got {value}")
    return value


def _host_source_base() -> tuple[bool, Path]:
    """Return a configured absolute host path or the legacy absolute fallback."""
    raw_value = os.getenv("HOST_SOURCE_BASE")
    if raw_value is None or not raw_value.strip():
        return False, Path("/mnt/user/tubearchives/bp")
    value = Path(raw_value.strip())
    if not value.is_absolute():
        raise ValueError(
            f"HOST_SOURCE_BASE must be an absolute host path, got {raw_value!r}"
        )
    return True, value


API_URL = os.getenv("API_URL", "http://localhost:8457/api")
API_TOKEN = os.getenv("API_TOKEN", "")
SCAN_INTERVAL = _positive_int("SCAN_INTERVAL", 60)
ALLOWED_IPS = _split_csv(os.getenv("ALLOWED_IPS", "127.0.0.1"))

SOURCE_DIR = Path(os.getenv("SOURCE_DIR", "/app/source"))
TARGET_DIR = Path(os.getenv("TARGET_DIR", "/app/target"))
HIDDEN_DIR = Path(os.getenv("HIDDEN_DIR", "/app/hidden"))
IMPORT_DIR = Path(os.getenv("IMPORT_DIR", "/app/import"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
# Salvage outputs are intentionally separate from originals and live under the
# persisted data mount by default.  They are never published over a source
# file, even when FFmpeg reports a completely clean result.
SALVAGE_DIR = Path(os.getenv("SALVAGE_DIR", str(DATA_DIR / "media-salvage")))
# Preserve the historical fallback for bare-Docker/local upgrades. Compose
# requires an explicit value, and app startup logs a warning when this fallback
# is active so it can be removed in a future breaking release.
HOST_SOURCE_BASE_CONFIGURED, HOST_SOURCE_BASE = _host_source_base()

DB_PATH = DATA_DIR / "videos.db"

FLASK_SECRET_KEY_ENV = os.getenv("FLASK_SECRET_KEY")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_LIFETIME_SECONDS = _positive_int("SESSION_LIFETIME_SECONDS", 8 * 3600)

# Pagination caps applied to API endpoints to prevent unbounded queries.
MAX_PER_PAGE = _positive_int("MAX_PER_PAGE", 500)
MAX_METADATA_PAGES = _positive_int("MAX_METADATA_PAGES", 10_000)
MAX_CONCURRENT_TRANSCODES = _positive_int("MAX_CONCURRENT_TRANSCODES", 1)
MAX_CONCURRENT_SALVAGE_JOBS = _positive_int("MAX_CONCURRENT_SALVAGE_JOBS", 1)
SALVAGE_JOB_TIMEOUT_SECONDS = _positive_int(
    "SALVAGE_JOB_TIMEOUT_SECONDS", 24 * 3600
)
SALVAGE_MAX_OUTPUT_BYTES = _positive_int(
    "SALVAGE_MAX_OUTPUT_BYTES", 100 * 1024 * 1024 * 1024
)
UNTRUNC_BINARY = os.getenv("UNTRUNC_BINARY", "untrunc").strip() or "untrunc"
UNTRUNC_JOB_TIMEOUT_SECONDS = _positive_int(
    "UNTRUNC_JOB_TIMEOUT_SECONDS", 24 * 3600
)
UNTRUNC_INSPECTION_TIMEOUT_SECONDS = _positive_int(
    "UNTRUNC_INSPECTION_TIMEOUT_SECONDS", 24 * 3600
)
UNTRUNC_MAX_OUTPUT_BYTES = _positive_int(
    "UNTRUNC_MAX_OUTPUT_BYTES", 100 * 1024 * 1024 * 1024
)

# Public-resource probes without account cookies. The application endpoints
# remain administrator-authenticated. The API key is optional corroboration;
# it is never passed to yt-dlp or stored.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
YTDLP_BINARY = os.getenv("YTDLP_BINARY", "yt-dlp").strip() or "yt-dlp"
AVAILABILITY_PROBE_TIMEOUT_SECONDS = _positive_int(
    "AVAILABILITY_PROBE_TIMEOUT_SECONDS", 45
)
AVAILABILITY_MAX_CONCURRENT_JOBS = _positive_int(
    "AVAILABILITY_MAX_CONCURRENT_JOBS", 1
)
AVAILABILITY_PROBE_RETRIES = _positive_int("AVAILABILITY_PROBE_RETRIES", 2)
AVAILABILITY_PROBE_DELAY_SECONDS = _nonnegative_float(
    "AVAILABILITY_PROBE_DELAY_SECONDS", 6.0
)
AVAILABILITY_RETRY_BACKOFF_SECONDS = _nonnegative_float(
    "AVAILABILITY_RETRY_BACKOFF_SECONDS", 1.0
)
AVAILABILITY_VIDEO_WORKERS = min(
    8, _positive_int("AVAILABILITY_VIDEO_WORKERS", 3)
)
AVAILABILITY_VIDEO_START_INTERVAL_SECONDS = _nonnegative_float(
    "AVAILABILITY_VIDEO_START_INTERVAL_SECONDS", 1.0
)

HEADERS = {"Authorization": f"Token {API_TOKEN}"} if API_TOKEN else {}

# Static folder shipped by the Astro build — resolved relative to repo root,
# not the cwd, so the entry script can be invoked from anywhere.
_PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PACKAGE_DIR.parent
STATIC_FOLDER = str(REPO_ROOT / "ui" / "dist")
