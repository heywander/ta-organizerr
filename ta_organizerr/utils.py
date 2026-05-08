"""Pure helpers: filename parsing, sanitization, path translation, validation."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .config import HOST_SOURCE_BASE, MAX_PER_PAGE, SOURCE_DIR

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
_YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_BRACKETED_ID_RE = re.compile(r"\[([a-zA-Z0-9_-]{11})\]$")
_FILENAME_INVALID_RE = re.compile(r'[\/:*?"<>|]')


def is_video(path: str | Path) -> bool:
    p = Path(path) if isinstance(path, str) else path
    return p.suffix.lower() in _VIDEO_EXTENSIONS


def is_valid_youtube_id(value: str) -> bool:
    return bool(value) and bool(_YOUTUBE_ID_RE.match(value))


def extract_id_from_filename(filename: str) -> Optional[str]:
    """Extract a YouTube video ID from a filename of form ``Title [ID].ext`` or ``ID.ext``."""
    stem = Path(filename).stem
    match = _BRACKETED_ID_RE.search(stem)
    if match:
        candidate = match.group(1)
        return candidate if is_valid_youtube_id(candidate) else None
    if is_valid_youtube_id(stem):
        return stem
    return None


def sanitize(text: str) -> str:
    text = text.encode("ascii", "ignore").decode()
    text = _FILENAME_INVALID_RE.sub("_", text)
    return text.strip()


def translate_ta_path(ta_path: Optional[str | Path]) -> Optional[Path]:
    """Map a TubeArchivist-internal path (``/youtube/Channel/...``) into ``SOURCE_DIR``."""
    if not ta_path:
        return None
    p = Path(ta_path)
    parts = list(p.parts)
    if len(parts) > 2 and parts[0] == "/":
        return SOURCE_DIR / Path(*parts[2:])
    return p


def translate_host_path(host_path: Optional[str | Path]) -> Optional[Path]:
    """Map a host filesystem path (``HOST_SOURCE_BASE/...``) into ``SOURCE_DIR``."""
    if not host_path:
        return None
    host_path_str = str(host_path)
    fixed_prefix = "/mnt/user/tubearchives/bp"
    if host_path_str.startswith(fixed_prefix):
        rel = host_path_str[len(fixed_prefix):].lstrip("/")
        return SOURCE_DIR / rel
    host_base_str = str(HOST_SOURCE_BASE)
    if host_path_str.startswith(host_base_str):
        rel = host_path_str[len(host_base_str):].lstrip("/")
        return SOURCE_DIR / rel
    return Path(host_path)


def path_within(candidate: Path, allowed_bases: Iterable[Path]) -> bool:
    """Return True iff ``candidate`` (after symlink resolution) lives inside one of ``allowed_bases``."""
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return False
    for base in allowed_bases:
        try:
            resolved.relative_to(base.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def clamp_pagination(page: object, per_page: object, default_per_page: int = 100) -> tuple[int, int]:
    """Coerce + bound pagination params from ``request.args``."""
    try:
        page_i = max(1, int(page) if page is not None else 1)
    except (TypeError, ValueError):
        page_i = 1
    try:
        per_i = int(per_page) if per_page is not None else default_per_page
    except (TypeError, ValueError):
        per_i = default_per_page
    per_i = max(1, min(per_i, MAX_PER_PAGE))
    return page_i, per_i
