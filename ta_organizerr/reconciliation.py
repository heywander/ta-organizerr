"""Unified, guarded reconciliation of tracked links and archive media."""
from __future__ import annotations

import ctypes
import copy
import errno
import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from .config import HIDDEN_DIR, HOST_SOURCE_BASE, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .library_lock import LIBRARY_MUTATION_LOCK
from .logging_util import log
from .recovery import scan_for_unindexed_videos
from .utils import extract_id_from_filename, is_video, path_within


MANAGED_ROOTS = (TARGET_DIR, HIDDEN_DIR)
MEDIA_ROOTS = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR)
_MAX_PATH_LENGTH = 4096
_PROBE_TIMEOUT_SECONDS = 20
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_PROBE_CACHE_SCHEMA = 1


def _bounded_env_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


_PROBE_WORKERS = _bounded_env_int("RECONCILIATION_PROBE_WORKERS", 2, 8)
_PROBE_CACHE_MAX = _bounded_env_int("RECONCILIATION_PROBE_CACHE_MAX", 4096, 50_000)

_DEMUXERS = {
    ".avi": "avi",
    ".m4v": "mov",
    ".mkv": "matroska",
    ".mov": "mov",
    ".mp4": "mov",
    ".ogv": "ogg",
    ".webm": "matroska",
}

_STATUS_LABELS = {
    "healthy": "Healthy",
    "repair_available": "Repair available from unindexed file",
    "multiple_matches": "Multiple matches—manual review",
    "invalid_media": "Invalid/corrupt media",
    "real_file_expected_symlink": "Real file where symlink expected",
    "truly_missing": "Truly missing",
}

_CACHE: dict[str, Any] | None = None
_PROBE_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_PROBE_CACHE_LOCK = threading.Lock()
_SCAN_STATE_LOCK = threading.Lock()
_SCAN_THREAD: threading.Thread | None = None
_SCAN_STATE: dict[str, Any] = {
    "job_id": None,
    "status": "idle",
    "phase": "idle",
    "current": 0,
    "total": 0,
    "detail": "No reconciliation scan has run yet.",
    "started_at": None,
    "completed_at": None,
    "error": None,
}


class ReconciliationError(RuntimeError):
    """An operator-facing reconciliation failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ReconciliationScanError(ReconciliationError):
    pass


def get_cached_inventory() -> dict[str, Any] | None:
    with _SCAN_STATE_LOCK:
        return _CACHE


def get_scan_state() -> dict[str, Any]:
    with _SCAN_STATE_LOCK:
        return dict(_SCAN_STATE)


def _clear_probe_cache() -> None:
    """Clear probe results; exposed for deterministic tests and diagnostics."""
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE.clear()


def _clear_negative_probe_cache() -> int:
    """Drop invalid results so an explicit rescan retries transient failures."""
    with _PROBE_CACHE_LOCK:
        stale = [
            key for key, result in _PROBE_CACHE.items()
            if not result.get("valid")
        ]
        for key in stale:
            _PROBE_CACHE.pop(key, None)
        return len(stale)


def _entry_key(path: Path) -> str:
    try:
        return str(path.parent.resolve(strict=False) / path.name)
    except (OSError, RuntimeError):
        return str(Path(os.path.abspath(path)))


def _lexically_under(path: Path, roots: Iterable[Path]) -> bool:
    candidate = Path(os.path.abspath(path))
    for root in roots:
        try:
            candidate.relative_to(Path(os.path.abspath(root)))
            return True
        except ValueError:
            continue
    return False


def _managed_root_for(path: Path) -> Path | None:
    candidate = Path(os.path.abspath(path))
    for configured in MANAGED_ROOTS:
        root = Path(os.path.abspath(configured))
        try:
            candidate.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _automatic_candidate_path_is_safe(candidate: Path, link_path: Path) -> bool:
    if _lexically_under(candidate, (SOURCE_DIR,)):
        return True
    candidate_root = _managed_root_for(candidate)
    link_root = _managed_root_for(link_path)
    # Relative managed links are portable to host consumers only inside the
    # same configured mount. Cross Target/Hidden matches remain recoverable
    # and previewable, but require manual handling without explicit host bases.
    return candidate_root is not None and candidate_root == link_root


def _managed_entry_is_safe(path: Path, *, require_parent: bool = True) -> bool:
    """Require a managed lexical path with no symlinked directory ancestor."""
    absolute = Path(os.path.abspath(path))
    matching_root: Path | None = None
    for configured in MANAGED_ROOTS:
        root = Path(os.path.abspath(configured))
        try:
            absolute.relative_to(root)
            matching_root = root
            break
        except ValueError:
            continue
    if matching_root is None or absolute == matching_root:
        return False

    try:
        relative_parent = absolute.parent.relative_to(matching_root)
    except ValueError:
        return False
    cursor = matching_root
    for component in (Path(), *[Path(part) for part in relative_parent.parts]):
        if component != Path():
            cursor /= component
        try:
            mode = os.lstat(cursor).st_mode
        except FileNotFoundError:
            if require_parent:
                return False
            continue
        except OSError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return False
    return True


def _source_file_is_safe(path: Path) -> bool:
    if not _lexically_under(path, (SOURCE_DIR,)):
        return False
    try:
        canonical = path.resolve(strict=True)
        entry_stat = os.lstat(path)
    except (OSError, RuntimeError):
        return False
    return stat.S_ISREG(entry_stat.st_mode) and path_within(canonical, (SOURCE_DIR,))


def _translate_link_target(link_path: Path, raw_target: str) -> Path:
    raw = Path(raw_target)
    if not raw.is_absolute():
        raw = link_path.parent / raw
    for host_prefix in (HOST_SOURCE_BASE, Path("/mnt/user/tubearchives/bp")):
        try:
            return SOURCE_DIR / raw.relative_to(host_prefix)
        except ValueError:
            continue
    return raw


def _walk_real_source_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    if not SOURCE_DIR.is_dir():
        return files
    for current, dirs, names in os.walk(SOURCE_DIR, followlinks=False):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if not os.path.islink(current_path / name)
        ]
        for name in names:
            path = current_path / name
            if not is_video(path) or not _source_file_is_safe(path):
                continue
            file_stat = os.lstat(path)
            files.append({
                "path": str(path),
                "filename": path.name,
                "video_id": extract_id_from_filename(path.name),
                "size_bytes": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
                "ctime_ns": file_stat.st_ctime_ns,
                "device": file_stat.st_dev,
                "inode": file_stat.st_ino,
                "kind": "source_file",
            })
    return sorted(files, key=lambda item: item["path"])


def _walk_managed_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for root, location in ((TARGET_DIR, "target"), (HIDDEN_DIR, "hidden")):
        if not root.is_dir():
            continue
        for current, dirs, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if not os.path.islink(current_path / name)
            ]
            for name in names:
                path = current_path / name
                try:
                    entry_stat = os.lstat(path)
                except OSError:
                    continue
                is_link = stat.S_ISLNK(entry_stat.st_mode)
                is_real_media = stat.S_ISREG(entry_stat.st_mode) and is_video(path)
                if not is_link and not is_real_media:
                    continue
                raw_target: str | None = None
                translated_target: str | None = None
                target_exists = False
                if is_link:
                    try:
                        raw_target = os.readlink(path)
                        translated = _translate_link_target(path, raw_target)
                        translated_target = str(translated)
                        target_exists = translated.is_file()
                    except OSError:
                        pass
                entries.append({
                    "path": str(path),
                    "entry_key": _entry_key(path),
                    "location": location,
                    "is_symlink": is_link,
                    "is_real_file": is_real_media,
                    "raw_target": raw_target,
                    "translated_target": translated_target,
                    "target_exists": target_exists,
                    "size_bytes": entry_stat.st_size if is_real_media else None,
                    "inode": entry_stat.st_ino,
                    "device": entry_stat.st_dev,
                    "mtime_ns": entry_stat.st_mtime_ns,
                    "ctime_ns": entry_stat.st_ctime_ns,
                })
    return sorted(entries, key=lambda item: item["path"])


def _probe_command(demuxer: str) -> list[str]:
    return [
        "ffprobe", "-v", "error",
        "-protocol_whitelist", "pipe",
        "-format_whitelist", demuxer,
        "-probesize", "10000000",
        "-analyzeduration", "10000000",
        "-f", demuxer,
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,duration",
        "-of", "json", "pipe:0",
    ]


def probe_media(path: Path, *, allowed_roots: Iterable[Path] | None = None) -> dict[str, Any]:
    """Inspect a regular file through a validated descriptor and pipe-only ffprobe."""
    result: dict[str, Any] = {
        "valid": False,
        "valid_media": False,
        "error": None,
        "duration": None,
        "streams": [],
        "has_video": False,
        "has_audio": False,
    }
    allowed_roots = tuple(allowed_roots or MEDIA_ROOTS)
    demuxer = _DEMUXERS.get(path.suffix.lower())
    if demuxer is None:
        result["error"] = "Unsupported media container"
        return result
    if not _lexically_under(path, allowed_roots):
        result["error"] = "Path outside allowed media directories"
        return result
    try:
        entry_stat = os.lstat(path)
        if not stat.S_ISREG(entry_stat.st_mode):
            result["error"] = "Candidate is not a real regular file"
            return result
        canonical = path.resolve(strict=True)
        if not path_within(canonical, allowed_roots):
            raise PermissionError
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(canonical, flags)
    except FileNotFoundError:
        result["error"] = "File not found"
        return result
    except PermissionError:
        result["error"] = "Path outside allowed media directories"
        return result
    except OSError as exc:
        result["error"] = f"Media file is not accessible: {exc}"
        return result

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            result["error"] = "Candidate is not a real regular file"
            return result
        if (opened_stat.st_dev, opened_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
            result["error"] = "Media file changed while it was being opened"
            return result
        result["size_bytes"] = opened_stat.st_size
        if opened_stat.st_size <= 0:
            result["error"] = "Media file is empty"
            return result
        proc_path = Path(f"/proc/self/fd/{fd}").resolve(strict=True)
        if not path_within(proc_path, allowed_roots):
            result["error"] = "Opened media escaped allowed directories"
            return result
        with os.fdopen(fd, "rb", closefd=False) as source:
            completed = subprocess.run(
                _probe_command(demuxer),
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
                close_fds=True,
                shell=False,
            )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            result["error"] = detail[:240] or "ffprobe could not inspect media"
            return result
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            result["error"] = "ffprobe returned invalid metadata"
            return result
        streams = []
        durations: list[float] = []
        for stream in payload.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            codec_type = stream.get("codec_type")
            if codec_type not in {"audio", "video"}:
                continue
            streams.append({
                "index": stream.get("index"),
                "type": codec_type,
                "codec": stream.get("codec_name"),
                "duration": stream.get("duration"),
            })
            try:
                duration = float(stream.get("duration"))
                if math.isfinite(duration) and duration > 0:
                    durations.append(duration)
            except (TypeError, ValueError):
                pass
        try:
            duration = float((payload.get("format") or {}).get("duration"))
            if math.isfinite(duration) and duration > 0:
                durations.append(duration)
        except (TypeError, ValueError):
            pass
        result["streams"] = streams
        result["has_video"] = any(item["type"] == "video" for item in streams)
        result["has_audio"] = any(item["type"] == "audio" for item in streams)
        result["duration"] = max(durations) if durations else None
        if not streams:
            result["error"] = "Media has no audio or video streams"
            return result
        if not durations:
            result["error"] = "Media duration is missing or zero"
            return result
        result["valid"] = result["valid_media"] = True
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "Timed out while inspecting media"
        return result
    except OSError as exc:
        result["error"] = f"Media inspection failed: {exc}"
        return result
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _structural_media_check(
    path: Path,
    expected_video_id: str | None,
) -> dict[str, Any]:
    """Cheaply validate an already-resolving link target without ffprobe.

    Full stream inspection is reserved for files involved in reconciliation
    decisions. A healthy link means its target is a confined, nonempty regular
    media entry with the exact tracked ID; it does not promise codec validity.
    """
    result: dict[str, Any] = {
        "valid": False,
        "valid_media": False,
        "error": None,
        "duration": None,
        "streams": [],
        "has_video": False,
        "has_audio": False,
        "inspection": "structural",
    }
    if _DEMUXERS.get(path.suffix.lower()) is None:
        result["error"] = "Symlink target has an unsupported media container"
        return result
    if not _lexically_under(path, MEDIA_ROOTS):
        result["error"] = "Symlink target is outside allowed media directories"
        return result
    try:
        entry_stat = os.lstat(path)
        if not stat.S_ISREG(entry_stat.st_mode):
            result["error"] = "Symlink target is not a real regular file"
            return result
        canonical = path.resolve(strict=True)
        if not path_within(canonical, MEDIA_ROOTS):
            result["error"] = "Symlink target escaped allowed media directories"
            return result
    except OSError as exc:
        result["error"] = f"Symlink target is not accessible: {exc}"
        return result
    result["size_bytes"] = entry_stat.st_size
    if entry_stat.st_size <= 0:
        result["error"] = "Symlink target is empty"
        return result
    actual_video_id = extract_id_from_filename(path.name)
    if expected_video_id and actual_video_id != expected_video_id:
        result["error"] = "Symlink target does not contain the exact tracked video ID"
        return result
    result["valid"] = result["valid_media"] = True
    return result


def _preview_fields(path: str) -> dict[str, str]:
    query = urlencode({"filepath": path})
    return {
        "preview_url": f"/api/recovery/preview?{query}",
        "preview_audio_url": f"/api/recovery/preview-audio?{query}",
    }


def _normalise_recovery_files(results: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for bucket in ("unindexed", "lost", "redundant", "rescue"):
        rows = results.get(bucket) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                continue
            path = Path(row["path"])
            try:
                file_stat = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode) or not _lexically_under(path, MEDIA_ROOTS):
                continue
            derived_id = extract_id_from_filename(path.name)
            reported_id = row.get("video_id")
            files.append({
                "path": str(path),
                "filename": path.name,
                # Automatic matching never trusts an ID supplied by a cached
                # recovery row; it is re-derived case-sensitively from the
                # current real filename.
                "video_id": derived_id,
                "reported_video_id": reported_id,
                "identity_error": (
                    "Recovery inventory ID does not match the current filename"
                    if reported_id and reported_id != derived_id else None
                ),
                "size_bytes": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
                "ctime_ns": file_stat.st_ctime_ns,
                "device": file_stat.st_dev,
                "inode": file_stat.st_ino,
                "bucket": bucket,
                "ta_source": row.get("ta_source"),
                "kind": row.get("type") or "recovery_file",
            })
    unique = {item["path"]: item for item in files}
    return sorted(unique.values(), key=lambda item: item["path"])


def _probe_ancestor_identity(
    path: Path,
    roots: Iterable[Path],
) -> tuple[Any, ...]:
    """Fingerprint the configured root and lexical directory ancestors."""
    absolute = Path(os.path.abspath(path))
    for configured in roots:
        root = Path(os.path.abspath(configured))
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            continue
        ancestors = [root]
        cursor = root
        for component in relative.parts[:-1]:
            cursor /= component
            ancestors.append(cursor)
        identity: list[tuple[Any, ...]] = []
        for ancestor in ancestors:
            try:
                ancestor_stat = os.lstat(ancestor)
                identity.append((
                    str(ancestor),
                    ancestor_stat.st_dev,
                    ancestor_stat.st_ino,
                    ancestor_stat.st_mode,
                ))
            except OSError:
                identity.append((str(ancestor), "missing"))
        return (str(root), tuple(identity))
    return ("outside", str(absolute.parent))


def _probe_cache_key(path: Path, roots: Iterable[Path]) -> tuple[Any, ...]:
    roots = tuple(roots)
    absolute = Path(os.path.abspath(path))
    root_key = tuple(str(Path(os.path.abspath(root))) for root in roots)
    demuxer = _DEMUXERS.get(path.suffix.lower())
    ancestor_identity = _probe_ancestor_identity(absolute, roots)
    try:
        file_stat = os.lstat(absolute)
        canonical = absolute.resolve(strict=True)
        return (
            _PROBE_CACHE_SCHEMA,
            str(absolute),
            str(canonical),
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
            ancestor_identity,
            root_key,
            demuxer,
        )
    except (OSError, RuntimeError):
        return (
            _PROBE_CACHE_SCHEMA,
            str(absolute),
            "missing-or-unresolved",
            ancestor_identity,
            root_key,
            demuxer,
        )


def _probe_cache_path_is_safe(path: Path, roots: Iterable[Path]) -> bool:
    """Re-authorize type and confinement before using any cached probe."""
    roots = tuple(roots)
    if _DEMUXERS.get(path.suffix.lower()) is None or not _lexically_under(path, roots):
        return False
    try:
        entry_stat = os.lstat(path)
        if not stat.S_ISREG(entry_stat.st_mode):
            return False
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return path_within(canonical, roots)


def _probe_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _PROBE_CACHE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if cached is None:
            return None
        _PROBE_CACHE.move_to_end(key)
        return copy.deepcopy(cached)


def _probe_cache_lookup(
    path: Path,
    roots: tuple[Path, ...],
    key: tuple[Any, ...],
) -> dict[str, Any] | None:
    cached = _probe_cache_get(key)
    if cached is None:
        return None
    if not _probe_cache_path_is_safe(path, roots):
        return None
    if _probe_cache_key(path, roots) != key:
        return None
    return cached


def _probe_cache_put(key: tuple[Any, ...], result: dict[str, Any]) -> None:
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[key] = copy.deepcopy(result)
        _PROBE_CACHE.move_to_end(key)
        while len(_PROBE_CACHE) > _PROBE_CACHE_MAX:
            _PROBE_CACHE.popitem(last=False)


def _probe_cached(
    path: Path,
    roots: Iterable[Path],
    cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    roots = tuple(roots)
    key = _probe_cache_key(path, roots)
    if key in cache:
        if _probe_cache_path_is_safe(path, roots) and _probe_cache_key(path, roots) == key:
            return copy.deepcopy(cache[key])
        cache.pop(key, None)
    result = _probe_cache_lookup(path, roots, key)
    if result is None:
        result = probe_media(path, allowed_roots=roots)
        _probe_cache_put(key, result)
    cache[key] = copy.deepcopy(result)
    return result


def _unexpected_probe_failure(exc: Exception) -> dict[str, Any]:
    return {
        "valid": False,
        "valid_media": False,
        "error": f"Media inspection failed unexpectedly: {exc}",
        "duration": None,
        "streams": [],
        "has_video": False,
        "has_audio": False,
    }


def _prewarm_probes(
    requests: Iterable[tuple[Path, tuple[Path, ...]]],
    cache: dict[tuple[Any, ...], dict[str, Any]],
    progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, int]:
    """Populate probe results with a fixed-size rolling executor window."""
    unique: dict[tuple[Any, ...], tuple[Path, tuple[Path, ...]]] = {}
    for path, roots in requests:
        key = _probe_cache_key(path, roots)
        unique.setdefault(key, (path, roots))

    total = len(unique)
    completed = 0
    hits = 0
    misses: list[tuple[tuple[Any, ...], Path, tuple[Path, ...]]] = []
    for key, (path, roots) in unique.items():
        result = _probe_cache_lookup(path, roots, key)
        if result is None:
            misses.append((key, path, roots))
            continue
        cache[key] = result
        hits += 1
        completed += 1
    if progress:
        progress("probing", completed, total, f"Validating reconciliation media ({completed}/{total})")

    if misses:
        worker_count = min(_PROBE_WORKERS, len(misses))
        iterator = iter(misses)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="reconciliation-probe",
        ) as executor:
            pending: dict[Any, tuple[tuple[Any, ...], Path, tuple[Path, ...]]] = {}

            def submit_next() -> bool:
                try:
                    key, path, roots = next(iterator)
                except StopIteration:
                    return False
                pending[executor.submit(probe_media, path, allowed_roots=roots)] = (
                    key,
                    path,
                    roots,
                )
                return True

            for _ in range(worker_count):
                submit_next()
            while pending:
                done, _not_done = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    key, _path, _roots = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - worker result is data
                        result = _unexpected_probe_failure(exc)
                    _probe_cache_put(key, result)
                    cache[key] = copy.deepcopy(result)
                    completed += 1
                    if progress:
                        progress(
                            "probing",
                            completed,
                            total,
                            f"Validating reconciliation media ({completed}/{total})",
                        )
                    submit_next()

    return {"requested": total, "hits": hits, "misses": len(misses)}


def _candidate(
    path_info: dict[str, Any],
    video_id: str,
    bucket_map: dict[str, str],
    probe_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    path = Path(path_info["path"])
    is_source = _lexically_under(path, (SOURCE_DIR,))
    probe_roots = (SOURCE_DIR,) if is_source else MANAGED_ROOTS
    probe = _probe_cached(path, probe_roots, probe_cache)
    candidate = {
        "path": str(path),
        "filename": path.name,
        "video_id": path_info.get("video_id"),
        "size_bytes": path_info.get("size_bytes"),
        "recovery_bucket": bucket_map.get(str(path)),
        "exact_video_id_match": path_info.get("video_id") == video_id,
        "eligible_for_repair": path_info.get("video_id") == video_id,
        "location": "source" if is_source else "managed",
        "valid": probe["valid"],
        "valid_media": probe["valid_media"],
        "error": probe["error"],
        "probe": probe,
        "duration_seconds": probe.get("duration"),
        "streams": probe.get("streams") or [],
        "has_video": bool(probe.get("has_video")),
        "has_audio": bool(probe.get("has_audio")),
        **_preview_fields(str(path)),
    }
    return candidate


def _broken_status(candidates: list[dict[str, Any]]) -> tuple[str, str]:
    # Ambiguity is about exact paths, not merely valid paths. A corrupt second
    # match still prevents automatic repair.
    if len(candidates) > 1:
        return "multiple_matches", f"{len(candidates)} exact video-ID files require manual review."
    if candidates:
        if candidates[0]["valid"]:
            bucket = candidates[0].get("recovery_bucket") or "source"
            return "repair_available", f"One valid exact-ID {bucket} file can repair this link."
        return "invalid_media", candidates[0]["error"] or "The exact-ID media candidate is invalid."
    return "truly_missing", "No exact video-ID file exists anywhere in the source inventory."


def _derive_orphan_id(fs_entry: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    path = Path(fs_entry["path"])
    link_id = extract_id_from_filename(path.name) or extract_id_from_filename(path.parent.name)
    target_id = None
    if fs_entry.get("raw_target"):
        target_id = extract_id_from_filename(Path(fs_entry["raw_target"]).name)
    return target_id or link_id, {"database": None, "link_path": link_id, "stored_target": target_id}


def _db_record(row: Any) -> dict[str, Any]:
    return {
        "video_id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "published": row["published"],
        "symlink": row["symlink"],
        "status": row["status"],
        "is_live": bool(row["is_live"]),
        "adopted_real": bool(row["adopted_real"]),
    }


def _probe_requests_for_inventory(
    database_entries: list[dict[str, Any]],
    fs_entries: list[dict[str, Any]],
    fs_by_key: dict[str, dict[str, Any]],
    source_by_id: dict[str, list[dict[str, Any]]],
) -> list[tuple[Path, tuple[Path, ...]]]:
    """Return only paths whose classification requires full stream probing."""
    requests: list[tuple[Path, tuple[Path, ...]]] = []
    represented_keys: set[str] = set()

    def add_candidates(video_id: str | None) -> None:
        if not video_id:
            return
        for item in source_by_id.get(video_id, []):
            path = Path(item["path"])
            roots = (SOURCE_DIR,) if _lexically_under(path, (SOURCE_DIR,)) else MANAGED_ROOTS
            requests.append((path, tuple(roots)))

    for row in database_entries:
        raw_link = row.get("symlink")
        key = _entry_key(Path(raw_link)) if isinstance(raw_link, str) else ""
        fs_entry = fs_by_key.get(key)
        if key:
            represented_keys.add(key)
        if fs_entry and fs_entry["is_real_file"]:
            requests.append((Path(fs_entry["path"]), tuple(MANAGED_ROOTS)))
        elif not (fs_entry and fs_entry["is_symlink"] and fs_entry["target_exists"]):
            add_candidates(row.get("video_id"))

    for fs_entry in fs_entries:
        if fs_entry["entry_key"] in represented_keys:
            continue
        if fs_entry["is_real_file"]:
            requests.append((Path(fs_entry["path"]), tuple(MANAGED_ROOTS)))
            continue
        orphan_id, _sources = _derive_orphan_id(fs_entry)
        if not (fs_entry["is_symlink"] and fs_entry["target_exists"]):
            add_candidates(orphan_id)

    return sorted(requests, key=lambda item: (str(item[0]), tuple(map(str, item[1]))))


def _classify_entry(
    db_record: dict[str, Any] | None,
    fs_entry: dict[str, Any] | None,
    source_by_id: dict[str, list[dict[str, Any]]],
    bucket_map: dict[str, str],
    owner_count: int,
    probe_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, Any]:
    link_path = (db_record or {}).get("symlink") or (fs_entry or {}).get("path")
    video_id = (db_record or {}).get("video_id")
    id_sources = {"database": video_id, "link_path": None, "stored_target": None}
    if fs_entry:
        orphan_id, orphan_sources = _derive_orphan_id(fs_entry)
        id_sources["link_path"] = orphan_sources["link_path"]
        id_sources["stored_target"] = orphan_sources["stored_target"]
        video_id = video_id or orphan_id
    candidate_infos = source_by_id.get(video_id, []) if video_id else []
    candidates: list[dict[str, Any]] = []
    status: str
    detail: str
    current_probe: dict[str, Any] | None = None

    if fs_entry and fs_entry["is_real_file"]:
        current_probe = _probe_cached(Path(fs_entry["path"]), MANAGED_ROOTS, probe_cache)
        current_path = Path(fs_entry["path"])
        current_id = extract_id_from_filename(current_path.name)
        candidates.append({
            "path": str(current_path),
            "filename": current_path.name,
            "video_id": current_id,
            "size_bytes": fs_entry.get("size_bytes"),
            "recovery_bucket": None,
            "exact_video_id_match": bool(video_id and current_id == video_id),
            "eligible_for_repair": False,
            "association": "expected_link_path",
            "location": "managed",
            "valid": current_probe["valid"],
            "valid_media": current_probe["valid_media"],
            "error": current_probe["error"],
            "probe": current_probe,
            "duration_seconds": current_probe.get("duration"),
            "streams": current_probe.get("streams") or [],
            "has_video": bool(current_probe.get("has_video")),
            "has_audio": bool(current_probe.get("has_audio")),
            **_preview_fields(str(current_path)),
        })
        if db_record and db_record["adopted_real"]:
            if current_probe["valid"]:
                status, detail = "healthy", "This valid real media file was explicitly adopted and is preserved in place."
            else:
                status, detail = "invalid_media", current_probe["error"] or "The adopted media file is invalid."
        else:
            status = "real_file_expected_symlink"
            detail = "A regular media file occupies the database-owned link path."
    elif fs_entry and fs_entry["is_symlink"] and fs_entry["target_exists"]:
        translated = Path(fs_entry["translated_target"])
        current_probe = _structural_media_check(translated, video_id)
        if current_probe["valid"]:
            status = "healthy"
            detail = (
                "The managed symlink resolves to a confined, nonempty regular "
                "file with the exact video ID. Stream validation is performed "
                "for reconciliation candidates."
            )
        else:
            status, detail = "invalid_media", current_probe["error"] or "The symlink target is structurally invalid."
    else:
        candidates = [
            _candidate(item, video_id, bucket_map, probe_cache)
            for item in candidate_infos
        ]
        status, detail = _broken_status(candidates)

    identity_values = {value for value in id_sources.values() if value is not None}
    identity_conflict = len(identity_values) > 1
    if identity_conflict:
        status = "multiple_matches"
        detail = "Database, link path, and stored target disagree on the exact video ID; manual review is required."

    unique_owner = bool(db_record and link_path and owner_count == 1)
    automatic_candidate_safe = bool(
        len(candidates) == 1
        and isinstance(link_path, str)
        and _automatic_candidate_path_is_safe(
            Path(candidates[0]["path"]), Path(link_path)
        )
    )
    allowed_actions: list[str] = []
    if candidates or (fs_entry and fs_entry["is_real_file"]):
        allowed_actions.append("preview_candidate")
    if (
        status == "repair_available"
        and unique_owner
        and len(candidates) == 1
        and candidates[0]["valid"]
        and (not fs_entry or fs_entry["is_symlink"])
        and not identity_conflict
        and isinstance(link_path, str)
        and _managed_entry_is_safe(Path(link_path))
        and automatic_candidate_safe
    ):
        allowed_actions.append("repair_link")
    if (
        status == "real_file_expected_symlink"
        and unique_owner
        and current_probe
        and current_probe["valid"]
        and not identity_conflict
    ):
        allowed_actions.append("adopt_existing_real_file")
    broken_symlink = bool(fs_entry and fs_entry["is_symlink"] and not fs_entry["target_exists"])
    if status == "truly_missing" and unique_owner and broken_symlink and not identity_conflict:
        allowed_actions.append("remove_broken_link")
    if status == "truly_missing" and unique_owner and fs_entry is None and not identity_conflict:
        allowed_actions.append("forget_stale_record")
    if owner_count > 1:
        detail += " Multiple database records own this path; all mutating actions are disabled."
    elif status == "repair_available" and candidates and not automatic_candidate_safe:
        detail += " The exact match is in the other managed mount; preview it, then repair manually because no portable host path is configured."

    link = {
        "path": link_path,
        "exists": fs_entry is not None,
        "is_symlink": bool(fs_entry and fs_entry["is_symlink"]),
        "is_real_file": bool(fs_entry and fs_entry["is_real_file"]),
        "raw_target": (fs_entry or {}).get("raw_target"),
        "stored_target_path": (fs_entry or {}).get("raw_target"),
        "translated_target": (fs_entry or {}).get("translated_target"),
        "target_exists": bool(fs_entry and fs_entry["target_exists"]),
        "current_probe": current_probe,
        "unique_database_owner": unique_owner,
    }
    return {
        "video_id": video_id,
        "title": (db_record or {}).get("title"),
        "channel": (db_record or {}).get("channel"),
        "status": status,
        "status_label": _STATUS_LABELS[status],
        "detail": detail,
        "link_path": link_path,
        "stored_target_path": link["stored_target_path"],
        "db_record": db_record,
        "link": link,
        "source_files": source_by_id.get(video_id, []) if video_id else [],
        "candidates": candidates,
        "video_id_sources": id_sources,
        "allowed_actions": allowed_actions,
        "identity_conflict": identity_conflict,
    }


def _inventory_token(payload: dict[str, Any]) -> str:
    stable = {
        key: payload[key]
        for key in ("database_entries", "link_entries", "source_files", "unindexed", "entries")
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _build_inventory_unlocked(
    recovery_provider: Callable[[], dict[str, Any]] | None = None,
    *,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, Any]:
    global _CACHE
    scan_started = time.perf_counter()
    recovery_provider = recovery_provider or scan_for_unindexed_videos
    if progress:
        progress("advanced_recovery", 0, 0, "Building the Advanced Recovery inventory")
    phase_started = time.perf_counter()
    try:
        recovery_results = recovery_provider()
    except Exception as exc:  # noqa: BLE001 - incomplete scans must fail closed
        raise ReconciliationScanError(
            f"Advanced Recovery inventory is unavailable: {exc}", 503
        ) from exc
    if not isinstance(recovery_results, dict):
        raise ReconciliationScanError("Advanced Recovery returned an invalid inventory", 503)
    recovery_seconds = time.perf_counter() - phase_started
    log(f"⏱️ [RECONCILIATION] Advanced Recovery inventory: {recovery_seconds:.2f}s")

    if progress:
        progress("discovery", 0, 0, "Reading database and filesystem inventory")
    phase_started = time.perf_counter()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT video_id, title, channel, published, symlink, status,
                   is_live, adopted_real
            FROM videos
            ORDER BY video_id
            """
        ).fetchall()
    database_entries = [_db_record(row) for row in rows]
    fs_entries = _walk_managed_entries()
    fs_by_key = {item["entry_key"]: item for item in fs_entries}
    source_files = _walk_real_source_files()
    recovery_files = _normalise_recovery_files(recovery_results)
    bucket_map = {item["path"]: item["bucket"] for item in recovery_files}
    source_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in source_files:
        if item["video_id"]:
            source_by_id.setdefault(item["video_id"], []).append(item)
    # Advanced Recovery can also identify an unindexed real file in
    # Target/Hidden. It remains an exact-ID candidate and therefore prevents a
    # false Truly Missing result. Repair creates a relative managed-tree link;
    # filename resemblance alone is never included here.
    known_candidate_paths = {item["path"] for item in source_files}
    for item in recovery_files:
        if not item.get("video_id") or item["path"] in known_candidate_paths:
            continue
        path = Path(item["path"])
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if (
            stat.S_ISREG(mode)
            and is_video(path)
            and _lexically_under(path, MANAGED_ROOTS)
            and path_within(path.resolve(strict=True), MANAGED_ROOTS)
        ):
            source_by_id.setdefault(item["video_id"], []).append(item)
            known_candidate_paths.add(item["path"])
    discovery_seconds = time.perf_counter() - phase_started
    log(
        "⏱️ [RECONCILIATION] Discovery: "
        f"{discovery_seconds:.2f}s; db={len(database_entries)}, "
        f"managed={len(fs_entries)}, source={len(source_files)}, "
        f"recovery={len(recovery_files)}"
    )

    path_owners: dict[str, int] = {}
    for row in database_entries:
        if isinstance(row["symlink"], str):
            key = _entry_key(Path(row["symlink"]))
            path_owners[key] = path_owners.get(key, 0) + 1

    probe_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    probe_requests = _probe_requests_for_inventory(
        database_entries,
        fs_entries,
        fs_by_key,
        source_by_id,
    )
    phase_started = time.perf_counter()
    probe_stats = _prewarm_probes(probe_requests, probe_cache, progress)
    probe_seconds = time.perf_counter() - phase_started
    log(
        "⏱️ [RECONCILIATION] Media validation: "
        f"{probe_seconds:.2f}s; requested={probe_stats['requested']}, "
        f"cache_hits={probe_stats['hits']}, probes={probe_stats['misses']}, "
        f"workers={min(_PROBE_WORKERS, probe_stats['misses'])}"
    )

    represented_db_keys = {
        _entry_key(Path(row["symlink"]))
        for row in database_entries
        if isinstance(row.get("symlink"), str)
    }
    classification_total = len(database_entries) + sum(
        1 for item in fs_entries if item["entry_key"] not in represented_db_keys
    )
    classified = 0
    if progress:
        progress("classifying", 0, classification_total, "Classifying link state")
    phase_started = time.perf_counter()
    entries: list[dict[str, Any]] = []
    represented_keys: set[str] = set()
    for row in database_entries:
        raw_link = row.get("symlink")
        key = _entry_key(Path(raw_link)) if isinstance(raw_link, str) else ""
        fs_entry = fs_by_key.get(key)
        if key:
            represented_keys.add(key)
        entries.append(_classify_entry(
            row, fs_entry, source_by_id, bucket_map, path_owners.get(key, 0), probe_cache
        ))
        classified += 1
        if progress:
            progress("classifying", classified, classification_total, "Classifying link state")
    for fs_entry in fs_entries:
        if fs_entry["entry_key"] in represented_keys:
            continue
        entries.append(_classify_entry(None, fs_entry, source_by_id, bucket_map, 0, probe_cache))
        classified += 1
        if progress:
            progress("classifying", classified, classification_total, "Classifying link state")

    entries.sort(key=lambda item: ((item.get("status") or ""), (item.get("video_id") or ""), (item.get("link_path") or "")))
    summary = {status: 0 for status in _STATUS_LABELS}
    for entry in entries:
        summary[entry["status"]] += 1
    summary.update({
        "tracked_database_entries": len(database_entries),
        "symlinks": sum(1 for item in fs_entries if item["is_symlink"]),
        "source_files": len(source_files),
        "unindexed": sum(1 for item in recovery_files if item["bucket"] == "unindexed"),
    })
    payload: dict[str, Any] = {
        "status": "complete",
        "scan_complete": True,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "entries": entries,
        "database_entries": database_entries,
        "link_entries": fs_entries,
        "source_files": source_files,
        "unindexed": [item for item in recovery_files if item["bucket"] == "unindexed"],
        "lost": [item for item in recovery_files if item["bucket"] == "lost"],
    }
    payload["scan_token"] = _inventory_token(payload)
    classify_seconds = time.perf_counter() - phase_started
    total_seconds = time.perf_counter() - scan_started
    log(
        "⏱️ [RECONCILIATION] Classification/token: "
        f"{classify_seconds:.2f}s; total={total_seconds:.2f}s; entries={len(entries)}"
    )
    with _SCAN_STATE_LOCK:
        _CACHE = payload
    return payload


def build_inventory(
    recovery_provider: Callable[[], dict[str, Any]] | None = None,
    *,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        return _build_inventory_unlocked(recovery_provider, progress=progress)


def _scan_progress(job_id: str, phase: str, current: int, total: int, detail: str) -> None:
    with _SCAN_STATE_LOCK:
        if _SCAN_STATE.get("job_id") != job_id or _SCAN_STATE.get("status") != "scanning":
            return
        _SCAN_STATE.update({
            "phase": phase,
            "current": current,
            "total": total,
            "detail": detail,
        })


def _run_background_scan(job_id: str, builder: Callable[..., dict[str, Any]]) -> None:
    global _CACHE
    try:
        inventory = builder(
            progress=lambda phase, current, total, detail: _scan_progress(
                job_id, phase, current, total, detail
            )
        )
    except Exception as exc:  # noqa: BLE001 - failure is exposed through scan state
        message = str(exc) or exc.__class__.__name__
        log(f"❌ [RECONCILIATION] Background scan failed: {message}")
        with _SCAN_STATE_LOCK:
            if _SCAN_STATE.get("job_id") == job_id:
                _SCAN_STATE.update({
                    "status": "error",
                    "phase": "error",
                    "detail": "Reconciliation scan failed.",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error": message,
                })
        return

    with _SCAN_STATE_LOCK:
        if _SCAN_STATE.get("job_id") != job_id:
            return
        _CACHE = inventory
        _SCAN_STATE.update({
            "status": "complete",
            "phase": "complete",
            "current": len(inventory.get("entries") or []),
            "total": len(inventory.get("entries") or []),
            "detail": "Reconciliation inventory is ready.",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        })


def _scan_wrapper(
    state: dict[str, Any],
    cache: dict[str, Any] | None,
    *,
    include_cache: bool,
) -> dict[str, Any]:
    payload = {
        "status": "scanning",
        "scan_complete": False,
        "scan": state,
        "stale": cache is not None,
    }
    if include_cache:
        payload["inventory"] = cache
    return payload


def _scan_error_wrapper(
    state: dict[str, Any],
    cache: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": "error",
        "scan_complete": False,
        "error": state.get("error") or "Reconciliation scan failed",
        "scan": state,
        "inventory": cache,
        "stale": cache is not None,
    }


def request_inventory(
    *,
    force: bool,
    include_cache: bool = True,
    builder: Callable[..., dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    """Return cached inventory or start/poll one single-flight background scan."""
    global _SCAN_THREAD
    builder = builder or build_inventory
    with _SCAN_STATE_LOCK:
        state = dict(_SCAN_STATE)
        cache = _CACHE
        if state["status"] == "scanning":
            return _scan_wrapper(state, cache, include_cache=include_cache), 202

        if not force and cache is not None and state["status"] != "error":
            response = dict(cache)
            response["scan"] = state
            response["stale"] = False
            return response, 200

        if not force and state["status"] == "error":
            return _scan_error_wrapper(state, cache), 503

        if force:
            cleared = _clear_negative_probe_cache()
            if cleared:
                log(
                    "🔄 [RECONCILIATION] Forced rescan will retry "
                    f"{cleared} cached invalid media result(s)"
                )

        job_id = secrets.token_hex(12)
        started_at = datetime.now(timezone.utc).isoformat()
        _SCAN_STATE.update({
            "job_id": job_id,
            "status": "scanning",
            "phase": "queued",
            "current": 0,
            "total": 0,
            "detail": "Reconciliation scan is queued.",
            "started_at": started_at,
            "completed_at": None,
            "error": None,
        })
        state = dict(_SCAN_STATE)
        try:
            _SCAN_THREAD = threading.Thread(
                target=_run_background_scan,
                args=(job_id, builder),
                daemon=True,
                name=f"reconciliation-scan-{job_id[:8]}",
            )
            _SCAN_THREAD.start()
        except Exception as exc:  # noqa: BLE001 - expose startup failure as state
            message = f"Could not start reconciliation scan: {exc}"
            _SCAN_THREAD = None
            _SCAN_STATE.update({
                "status": "error",
                "phase": "error",
                "detail": "Reconciliation scan could not start.",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": message,
            })
            state = dict(_SCAN_STATE)
            log(f"❌ [RECONCILIATION] {message}")
            return _scan_error_wrapper(state, cache), 503
        return _scan_wrapper(state, cache, include_cache=include_cache), 202


def _reset_scan_runtime_for_tests(*, clear_inventory: bool = True) -> None:
    """Reset completed scan state; a live background scan may not be discarded."""
    global _CACHE, _SCAN_THREAD
    with _SCAN_STATE_LOCK:
        if _SCAN_THREAD is not None and _SCAN_THREAD.is_alive():
            raise RuntimeError("Cannot reset reconciliation state during an active scan")
        if clear_inventory:
            _CACHE = None
        _SCAN_THREAD = None
        _SCAN_STATE.update({
            "job_id": None,
            "status": "idle",
            "phase": "idle",
            "current": 0,
            "total": 0,
            "detail": "No reconciliation scan has run yet.",
            "started_at": None,
            "completed_at": None,
            "error": None,
        })


def _require_selector(data: dict[str, Any]) -> tuple[str, str, str]:
    video_id = data.get("video_id")
    link_path = data.get("link_path")
    scan_token = data.get("scan_token")
    if not all(isinstance(value, str) and value for value in (video_id, link_path, scan_token)):
        raise ReconciliationError("video_id, link_path, and scan_token are required", 400)
    if len(link_path) > _MAX_PATH_LENGTH:
        raise ReconciliationError("link_path is too long", 400)
    return video_id, link_path, scan_token


def _fresh_entry(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    video_id, link_path, scan_token = _require_selector(data)
    inventory = _build_inventory_unlocked()
    if inventory["scan_token"] != scan_token:
        raise ReconciliationError("Inventory changed; rescan before acting", 409)
    matches = [
        entry for entry in inventory["entries"]
        if entry.get("video_id") == video_id and entry.get("link_path") == link_path
    ]
    if len(matches) != 1:
        raise ReconciliationError("Tracked entry is stale or ambiguous", 409)
    supplied_target = data.get("stored_target_path")
    if supplied_target is not None and supplied_target != matches[0].get("stored_target_path"):
        raise ReconciliationError("Stored target selector is stale or tampered", 409)
    return inventory, matches[0]


def _assert_unique_db_owner(video_id: str, link_path: str) -> None:
    with get_db() as conn:
        owners = conn.execute(
            "SELECT video_id FROM videos WHERE symlink = ?", (link_path,)
        ).fetchall()
    if len(owners) != 1 or owners[0]["video_id"] != video_id:
        raise ReconciliationError("Link path does not have one authoritative database owner", 409)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing any destination (Linux renameat2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ReconciliationError("Safe filesystem quarantine is unavailable", 503)
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(destination), _RENAME_NOREPLACE
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(source))


def _quarantine_symlink(
    path: Path, expected_target: str | None
) -> tuple[Path, str, tuple[int, int]]:
    if not _managed_entry_is_safe(path):
        raise ReconciliationError("Unsafe managed link path", 400)
    quarantine_dir = path.parent / f".reconcile-quarantine-{secrets.token_hex(16)}"
    try:
        quarantine_dir.mkdir(mode=0o700)
        quarantine_dir.chmod(0o700)
    except OSError as exc:
        raise ReconciliationError(f"Could not create private link quarantine: {exc}", 503) from exc
    quarantine = quarantine_dir / "entry"
    try:
        _rename_noreplace(path, quarantine)
    except FileNotFoundError as exc:
        quarantine_dir.rmdir()
        raise ReconciliationError("Link changed since the inventory scan", 409) from exc
    except OSError as exc:
        try:
            quarantine_dir.rmdir()
        except OSError:
            pass
        raise ReconciliationError(f"Could not safely quarantine link: {exc}", 409) from exc
    try:
        mode = os.lstat(quarantine).st_mode
        if not stat.S_ISLNK(mode):
            _restore_quarantine(quarantine, path)
            raise ReconciliationError("Refusing to mutate a real filesystem entry", 409)
        raw_target = os.readlink(quarantine)
        quarantine_stat = os.lstat(quarantine)
        if expected_target is not None and raw_target != expected_target:
            _restore_quarantine(quarantine, path)
            raise ReconciliationError("Link target changed since the inventory scan", 409)
        translated = _translate_link_target(path, raw_target)
        if translated.is_file():
            _restore_quarantine(quarantine, path)
            raise ReconciliationError("Link is no longer broken", 409)
        return quarantine, raw_target, (quarantine_stat.st_dev, quarantine_stat.st_ino)
    except ReconciliationError:
        raise
    except OSError as exc:
        try:
            _restore_quarantine(quarantine, path)
        except OSError:
            pass
        raise ReconciliationError(f"Could not verify quarantined link: {exc}", 409) from exc


def _discard_quarantined_symlink(
    path: Path, expected_identity: tuple[int, int] | None = None
) -> None:
    """Delete only the secret-name symlink moved there by this process.

    The application serializes its own writers and uses an unguessable name.
    A same-UID external writer could theoretically race the final unlink, so
    we additionally require the original inode immediately beforehand and
    fail closed if either identity or type differs.
    """
    private_quarantine = path.name == "entry" and path.parent.name.startswith(
        ".reconcile-quarantine-"
    )
    directory_fd: int | None = None
    if private_quarantine:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(path.parent, flags)
        entry_stat = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    else:
        entry_stat = os.lstat(path)
    if not stat.S_ISLNK(entry_stat.st_mode):
        if directory_fd is not None:
            os.close(directory_fd)
        raise ReconciliationError("Quarantined entry changed type; refusing deletion", 409)
    if expected_identity is not None and (
        entry_stat.st_dev, entry_stat.st_ino
    ) != expected_identity:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ReconciliationError("Quarantined link identity changed; refusing deletion", 409)
    try:
        if directory_fd is not None:
            os.unlink(path.name, dir_fd=directory_fd)
        else:
            path.unlink()
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    if private_quarantine:
        path.parent.rmdir()


def _restore_quarantine(quarantine: Path | None, destination: Path) -> None:
    if quarantine is None or not os.path.lexists(quarantine):
        return
    if os.path.lexists(destination):
        return
    _rename_noreplace(quarantine, destination)
    if quarantine.name == "entry" and quarantine.parent.name.startswith(
        ".reconcile-quarantine-"
    ):
        quarantine.parent.rmdir()


def _finish_action(message: str) -> dict[str, Any]:
    global _CACHE
    refresh_started_at = datetime.now(timezone.utc).isoformat()
    try:
        inventory = _build_inventory_unlocked()
        return {
            "success": True,
            "action_applied": True,
            "message": message,
            "inventory": inventory,
        }
    except Exception as exc:  # noqa: BLE001 - mutation has already committed
        # The mutation is already committed and was logged by the caller.
        # Any cached pre-mutation snapshot is now unsafe to serve as fresh.
        raw_error = str(exc) or exc.__class__.__name__
        state_error = (
            "Action succeeded, but the follow-up reconciliation refresh "
            f"failed: {raw_error}"
        )
        with _SCAN_STATE_LOCK:
            _CACHE = None
            _SCAN_STATE.update({
                "job_id": secrets.token_hex(12),
                "status": "error",
                "phase": "error",
                "current": 0,
                "total": 0,
                "detail": "Action applied; follow-up inventory refresh failed.",
                "started_at": refresh_started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": state_error,
            })
        log(f"❌ [RECONCILIATION] {state_error}")
        return {
            "success": True,
            "action_applied": True,
            "message": message,
            "rescan_error": raw_error,
            "inventory": None,
        }


def repair_link(data: dict[str, Any]) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        _inventory, entry = _fresh_entry(data)
        if "repair_link" not in entry["allowed_actions"]:
            raise ReconciliationError("This entry is not eligible for automatic repair", 409)
        candidate_path = data.get("candidate_path")
        if not isinstance(candidate_path, str) or len(candidate_path) > _MAX_PATH_LENGTH:
            raise ReconciliationError("candidate_path is required", 400)
        candidates = entry["candidates"]
        if len(candidates) != 1 or candidates[0]["path"] != candidate_path:
            raise ReconciliationError("Candidate is stale, ambiguous, or was not an exact-ID match", 409)
        video_id, link_path_raw, _token = _require_selector(data)
        link_path = Path(link_path_raw)
        candidate = Path(candidate_path)
        _assert_unique_db_owner(video_id, link_path_raw)
        is_source_candidate = _source_file_is_safe(candidate)
        is_managed_candidate = False
        if _lexically_under(candidate, MANAGED_ROOTS) and _managed_entry_is_safe(candidate):
            try:
                is_managed_candidate = stat.S_ISREG(os.lstat(candidate).st_mode)
            except OSError:
                is_managed_candidate = False
        if (
            not (is_source_candidate or is_managed_candidate)
            or extract_id_from_filename(candidate.name) != video_id
            or _entry_key(candidate) == _entry_key(link_path)
            or (
                is_managed_candidate
                and _managed_root_for(candidate) != _managed_root_for(link_path)
            )
        ):
            raise ReconciliationError("Candidate is not a confined exact video-ID real file", 400)
        probe_roots = (SOURCE_DIR,) if is_source_candidate else MANAGED_ROOTS
        probe = probe_media(candidate, allowed_roots=probe_roots)
        if not probe["valid"]:
            raise ReconciliationError(probe["error"] or "Candidate media is invalid", 422)
        if not _managed_entry_is_safe(link_path):
            raise ReconciliationError("Unsafe managed link path", 400)

        quarantine: Path | None = None
        if os.path.lexists(link_path):
            quarantine, _raw, quarantine_identity = _quarantine_symlink(
                link_path, entry["stored_target_path"]
            )
        else:
            quarantine_identity = None
        try:
            if is_source_candidate:
                link_target = HOST_SOURCE_BASE / candidate.relative_to(SOURCE_DIR)
            else:
                link_target = Path(os.path.relpath(candidate, start=link_path.parent))
            os.symlink(str(link_target), link_path)
            with get_db() as conn:
                cursor = conn.execute(
                    """
                    UPDATE videos SET status = 'linked', adopted_real = 0,
                                      last_updated = CURRENT_TIMESTAMP
                    WHERE video_id = ? AND symlink = ?
                    """,
                    (video_id, link_path_raw),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ReconciliationError("Database owner changed during repair", 409)
                conn.commit()
        except Exception:
            if os.path.islink(link_path):
                replacement = link_path.parent / f".reconcile-rollback-{secrets.token_hex(16)}"
                try:
                    _rename_noreplace(link_path, replacement)
                    _discard_quarantined_symlink(replacement)
                except (OSError, ReconciliationError):
                    pass
            try:
                _restore_quarantine(quarantine, link_path)
            except OSError:
                pass
            raise
        if quarantine is not None:
            _discard_quarantined_symlink(quarantine, quarantine_identity)
        log(f"🔗 [RECONCILIATION] Repaired {video_id}: {link_path} -> {link_target}")
        return _finish_action("Link repaired from the exact-ID source candidate")


def adopt_existing_real_file(data: dict[str, Any]) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        _inventory, entry = _fresh_entry(data)
        if "adopt_existing_real_file" not in entry["allowed_actions"]:
            raise ReconciliationError("This entry is not eligible for adoption", 409)
        video_id, link_path_raw, _token = _require_selector(data)
        link_path = Path(link_path_raw)
        _assert_unique_db_owner(video_id, link_path_raw)
        if not _managed_entry_is_safe(link_path):
            raise ReconciliationError("Unsafe managed media path", 400)
        try:
            mode = os.lstat(link_path).st_mode
        except OSError as exc:
            raise ReconciliationError("Real media file changed since the inventory scan", 409) from exc
        if not stat.S_ISREG(mode):
            raise ReconciliationError("Adoption never replaces or follows a symlink", 409)
        probe = probe_media(link_path, allowed_roots=MANAGED_ROOTS)
        if not probe["valid"]:
            raise ReconciliationError(probe["error"] or "Real media is invalid", 422)
        with get_db() as conn:
            cursor = conn.execute(
                """
                UPDATE videos SET adopted_real = 1, status = 'adopted',
                                  last_updated = CURRENT_TIMESTAMP
                WHERE video_id = ? AND symlink = ?
                """,
                (video_id, link_path_raw),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise ReconciliationError("Database owner changed during adoption", 409)
            conn.commit()
        log(f"📌 [RECONCILIATION] Adopted real media in place for {video_id}: {link_path}")
        return _finish_action("Existing real media adopted in place; no file bytes were moved or deleted")


def remove_broken_link(data: dict[str, Any]) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        _inventory, entry = _fresh_entry(data)
        if "remove_broken_link" not in entry["allowed_actions"]:
            raise ReconciliationError("Only a uniquely owned, truly missing broken symlink can be removed", 409)
        video_id, link_path_raw, _token = _require_selector(data)
        link_path = Path(link_path_raw)
        _assert_unique_db_owner(video_id, link_path_raw)
        quarantine, raw_target, quarantine_identity = _quarantine_symlink(
            link_path, entry["stored_target_path"]
        )
        try:
            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "DELETE FROM videos WHERE video_id = ? AND symlink = ?",
                    (video_id, link_path_raw),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    raise ReconciliationError("Database owner changed during removal", 409)
                _discard_quarantined_symlink(quarantine, quarantine_identity)
                try:
                    conn.commit()
                except Exception:
                    # Only a symlink was discarded; recreate that symlink and
                    # restore it without overwriting any raced-in entry if the
                    # database commit fails.
                    os.symlink(raw_target, link_path)
                    raise
        except Exception:
            try:
                _restore_quarantine(quarantine, link_path)
            except OSError:
                pass
            raise
        log(f"🧹 [RECONCILIATION] Removed broken symlink and stale record for {video_id}: {link_path}")
        return _finish_action("Broken symlink and corresponding stale database record removed")


def forget_stale_record(data: dict[str, Any]) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        _inventory, entry = _fresh_entry(data)
        if "forget_stale_record" not in entry["allowed_actions"]:
            raise ReconciliationError("Only a uniquely owned missing-path record can be forgotten", 409)
        video_id, link_path_raw, _token = _require_selector(data)
        _assert_unique_db_owner(video_id, link_path_raw)
        if os.path.lexists(link_path_raw):
            raise ReconciliationError("A filesystem entry now exists at the tracked path", 409)
        with get_db() as conn:
            cursor = conn.execute(
                "DELETE FROM videos WHERE video_id = ? AND symlink = ?",
                (video_id, link_path_raw),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise ReconciliationError("Database owner changed during removal", 409)
            conn.commit()
        log(f"🗂️ [RECONCILIATION] Forgot stale record for {video_id}: {link_path_raw}")
        return _finish_action("Stale database record forgotten; no filesystem entry was deleted")


def preview_candidate(data: dict[str, Any]) -> dict[str, Any]:
    with LIBRARY_MUTATION_LOCK:
        _inventory, entry = _fresh_entry(data)
        candidate_path = data.get("candidate_path")
        if not isinstance(candidate_path, str) or len(candidate_path) > _MAX_PATH_LENGTH:
            raise ReconciliationError("candidate_path is required", 400)
        permitted = {candidate["path"] for candidate in entry["candidates"]}
        if entry["link"]["is_real_file"]:
            permitted.add(entry["link_path"])
        if candidate_path not in permitted:
            raise ReconciliationError("Candidate does not belong to this inventory entry", 409)
        path = Path(candidate_path)
        if _lexically_under(path, (SOURCE_DIR,)):
            roots = (SOURCE_DIR,)
        elif _lexically_under(path, MANAGED_ROOTS):
            roots = MANAGED_ROOTS
        else:
            raise ReconciliationError("Candidate escaped allowed media roots", 409)
        probe = probe_media(path, allowed_roots=roots)
        if not probe["valid"]:
            raise ReconciliationError(probe["error"] or "Candidate media is invalid", 422)
        return {
            "success": True,
            "video_id": entry["video_id"],
            "candidate_path": candidate_path,
            "probe": probe,
            "exact_video_id_match": extract_id_from_filename(path.name) == entry["video_id"],
            **_preview_fields(candidate_path),
        }
