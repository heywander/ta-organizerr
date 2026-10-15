"""Pure helpers: filename parsing, sanitization, path translation, validation."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from .config import HOST_SOURCE_BASE, MAX_PER_PAGE, SOURCE_DIR

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
_YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_BRACKETED_ID_RE = re.compile(r"\[([a-zA-Z0-9_-]{11})\]$")
# Characters that are invalid on at least one common filesystem (NTFS/SMB,
# ext4, APFS), plus ASCII control characters.
_FILENAME_INVALID_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_MAX_NAME_LENGTH = 200


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


def sanitize(text: str, fallback: str = "") -> str:
    """Turn arbitrary text into a safe single path component.

    Unicode is preserved (NFC-normalized) so non-Latin channel names keep
    their real titles instead of collapsing to an empty string. Only
    filesystem-invalid characters are replaced, leading/trailing dots and
    whitespace are stripped (``.`` / ``..`` would otherwise escape the
    folder), and the result is capped to a sane length. ``fallback`` is
    returned when nothing usable is left.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = _FILENAME_INVALID_RE.sub("_", text)
    text = text.strip().strip(".").strip()
    if len(text) > _MAX_NAME_LENGTH:
        text = text[:_MAX_NAME_LENGTH].rstrip()
    return text or fallback


def _relative_to(candidate: Path, base: Path) -> Path | None:
    try:
        return candidate.relative_to(base)
    except ValueError:
        return None


def translate_ta_path(ta_path: Optional[str | Path]) -> Optional[Path]:
    """Map a TubeArchivist-internal path (``/youtube/Channel/...``) into ``SOURCE_DIR``."""
    if not ta_path:
        return None
    p = Path(ta_path)
    host_path = translate_host_path(p)
    if host_path != p:
        return host_path
    if _relative_to(p, SOURCE_DIR) is not None:
        return p
    if not p.is_absolute():
        return SOURCE_DIR / p
    parts = list(p.parts)
    if len(parts) > 2 and parts[0] == "/":
        return SOURCE_DIR / Path(*parts[2:])
    return p


def translate_host_path(host_path: Optional[str | Path]) -> Optional[Path]:
    """Map a host filesystem path (``HOST_SOURCE_BASE/...``) into ``SOURCE_DIR``."""
    if not host_path:
        return None
    p = Path(host_path)
    if _relative_to(p, SOURCE_DIR) is not None:
        return p
    rel = _relative_to(p, HOST_SOURCE_BASE)
    if rel is not None:
        return SOURCE_DIR / rel
    return p


def translated_symlink_target(path: Path) -> Optional[Path]:
    """Return a symlink target translated into container paths when possible."""
    try:
        raw_target = Path(os.readlink(path))
    except OSError:
        return None
    if not raw_target.is_absolute():
        raw_target = path.parent / raw_target
    return translate_host_path(raw_target)


def resolve_media_source(path: str | Path) -> Optional[Path]:
    """Resolve a user-facing media path to the file the app should read/write."""
    candidate = Path(path)
    if os.path.islink(candidate):
        return translated_symlink_target(candidate)
    return translate_host_path(candidate)


def path_within(candidate: Path, allowed_bases: Iterable[Path], *, follow_symlinks: bool = True) -> bool:
    """Return True iff ``candidate`` lives inside one of ``allowed_bases``.

    ``follow_symlinks=False`` validates the path entry itself, which is needed
    for operations that delete or inspect a symlink without trusting its target.
    """
    try:
        resolved = candidate.resolve(strict=False) if follow_symlinks else Path(os.path.abspath(candidate))
    except OSError:
        return False
    for base in allowed_bases:
        try:
            resolved.relative_to(base.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


def path_in_allowed_tree(candidate: Path, allowed_bases: Iterable[Path]) -> bool:
    """Validate the directory entry without following the final symlink."""
    return path_within(candidate, allowed_bases, follow_symlinks=False)


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
