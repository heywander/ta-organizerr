"""Non-destructive diagnosis and best-effort salvage of damaged media.

This module deliberately does not modify or "heal" the input.  FFmpeg reads an
already-open, validated descriptor and writes to a private partial file under
``SALVAGE_DIR``.  A result is published only after it is non-empty, probeable,
and fully decoded once more.  Remux/re-encode can preserve decodable packets;
they cannot reconstruct bytes that no longer exist.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import queue
import re
import secrets
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    HIDDEN_DIR,
    IMPORT_DIR,
    MAX_CONCURRENT_SALVAGE_JOBS,
    SALVAGE_DIR,
    SALVAGE_JOB_TIMEOUT_SECONDS,
    SALVAGE_MAX_OUTPUT_BYTES,
    SOURCE_DIR,
    TARGET_DIR,
)
from .library_lock import LIBRARY_MUTATION_LOCK
from .logging_util import log
from .media_reservations import (
    MediaReservation,
    MediaReservationConflict,
    extend_media_reservation,
    media_identity,
    release_media,
    reserve_media,
)
from .utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    path_in_allowed_tree,
    path_within,
    resolve_media_source,
)


MEDIA_ROOTS = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR, SALVAGE_DIR)
_CANDIDATE_ROOTS = (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR, IMPORT_DIR, SALVAGE_DIR)
_DEMUXERS = {
    ".aac": "aac",
    ".avi": "avi",
    ".flac": "flac",
    ".m4a": "mov",
    ".m4v": "mov",
    ".mkv": "matroska",
    ".mov": "mov",
    ".mp3": "mp3",
    ".mp4": "mov",
    ".oga": "ogg",
    ".ogg": "ogg",
    ".ogv": "ogg",
    ".opus": "ogg",
    ".wav": "wav",
    ".webm": "matroska",
}
_MAX_PATH_LENGTH = 4096
_MAX_EXPECTED_DURATION_SECONDS = 31 * 24 * 3600
_MAX_JOB_HISTORY = 30
_MAX_CANDIDATES = 20
_MAX_CANDIDATE_SCAN_ENTRIES = 100_000
_CANDIDATE_BUDGET_SECONDS = 15.0
_STDERR_TAIL_LINES = 12
_MAX_STREAMS_TO_DECODE = 8
_MAX_LOG_LINE_CHARS = 512
_OUTPUT_FREE_RESERVE_BYTES = 64 * 1024 * 1024
_CANDIDATE_SNAPSHOT_ERROR_PREFIX = "Candidate snapshot changed"
_ERROR_MARKERS = (
    "error",
    "corrupt",
    "invalid",
    "damaged",
    "concealing",
    "truncat",
    "missing picture",
    "moov atom not found",
)


class SalvageError(Exception):
    """Expected validation or media-processing failure."""


class JobCancelled(SalvageError):
    """Raised inside a worker after a requested cancellation."""


@dataclass
class OpenedMedia:
    requested_path: str
    resolved_path: Path
    fd: int
    identity: tuple[int, int]
    size: int
    mtime_ns: int
    ctime_ns: int
    demuxer: str
    suffix: str

    @property
    def proc_path(self) -> str:
        return f"/proc/self/fd/{self.fd}"

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


@dataclass(frozen=True)
class CandidateScanEntry:
    path: Path
    enumerated_resolved_path: Path
    entry_type: str
    expected_identity: tuple[int, int]
    expected_video_id: str


@dataclass
class JobRuntime:
    sources: list[OpenedMedia]
    reservation: MediaReservation | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[str] | None = None
    partial_path: Path | None = None
    deadline: float = field(
        default_factory=lambda: time.monotonic() + SALVAGE_JOB_TIMEOUT_SECONDS
    )


_jobs: dict[str, dict[str, Any]] = {}
_runtimes: dict[str, JobRuntime] = {}
_active_identities: set[tuple[int, int]] = set()
_jobs_lock = threading.RLock()
_audit_lock = threading.Lock()
_candidate_scan_guard = threading.BoundedSemaphore(value=1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_expected_duration(value: object) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "expected_duration_seconds must be a positive number"
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, "expected_duration_seconds must be a positive number"
    if not math.isfinite(parsed) or parsed <= 0:
        return None, "expected_duration_seconds must be a positive number"
    if parsed > _MAX_EXPECTED_DURATION_SECONDS:
        return None, "expected_duration_seconds exceeds the 31-day limit"
    return parsed, None


def parse_expected_sha256(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        return None, "expected_source_sha256 must be the 64-character diagnosis SHA-256"
    return value.lower(), None


def _source_error(filepath: object) -> str | None:
    if not isinstance(filepath, str) or not filepath:
        return "source_path must be a non-empty path string"
    if "\x00" in filepath or len(filepath) > _MAX_PATH_LENGTH:
        return "Invalid source path"
    if not path_in_allowed_tree(Path(filepath), MEDIA_ROOTS):
        return "Path outside allowed media directories"
    return None


def _open_media(filepath: object) -> OpenedMedia:
    error = _source_error(filepath)
    if error:
        raise SalvageError(error)
    assert isinstance(filepath, str)
    translated = resolve_media_source(filepath)
    if translated is None:
        raise SalvageError("Could not resolve media source")
    try:
        canonical = translated.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SalvageError(f"Could not resolve media source: {exc}") from None
    if not path_within(canonical, MEDIA_ROOTS):
        raise SalvageError("Resolved media path outside allowed media directories")
    suffix = canonical.suffix.lower()
    demuxer = _DEMUXERS.get(suffix)
    if demuxer is None:
        raise SalvageError(f"Unsupported media container: {suffix or '(none)'}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(canonical, flags)
    except OSError as exc:
        raise SalvageError(f"Could not open media source: {exc}") from None
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SalvageError("Media source is not a regular file")
        if file_stat.st_size <= 0:
            raise SalvageError("Media source is empty")
        try:
            opened_path = Path(f"/proc/self/fd/{fd}").resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise SalvageError("Could not verify opened media source") from None
        if not path_within(opened_path, MEDIA_ROOTS):
            raise SalvageError("Opened media source escaped allowed directories")
        return OpenedMedia(
            requested_path=filepath,
            resolved_path=opened_path,
            fd=fd,
            identity=(file_stat.st_dev, file_stat.st_ino),
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            ctime_ns=file_stat.st_ctime_ns,
            demuxer=demuxer,
            suffix=suffix,
        )
    except Exception:
        os.close(fd)
        raise


def _video_id_from_path_evidence(
    requested_path: str | Path,
    resolved_path: str | Path,
) -> str | None:
    """Return the one unambiguous exact ID supplied by entry/target names.

    Either side may be generic (for example ``video.mp4`` or an opaque source
    filename), and duplicate evidence for the same ID is harmless. Different
    non-null IDs are contradictory and must never be silently OR-matched.
    """
    requested_id = extract_id_from_filename(Path(requested_path).name)
    resolved_id = extract_id_from_filename(Path(resolved_path).name)
    evidence = {item for item in (requested_id, resolved_id) if item is not None}
    if len(evidence) > 1:
        raise SalvageError(
            "Conflicting exact video IDs in the requested entry and resolved media target"
        )
    return next(iter(evidence), None)


def _media_video_id(media: OpenedMedia) -> str | None:
    return _video_id_from_path_evidence(media.requested_path, media.resolved_path)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(job)


def _update_job(job_id: str, **values: Any) -> None:
    if "progress" in values:
        values.setdefault("progress_percent", values["progress"])
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(values)


def _audit(event: str, job: dict[str, Any], **details: Any) -> None:
    record = {
        "timestamp": _now(),
        "event": event,
        "job_id": job.get("id"),
        "kind": job.get("kind"),
        "mode": job.get("mode"),
        "source_path": job.get("source_path"),
        "candidate_path": job.get("candidate_path"),
        "output_path": job.get("output_path"),
        **details,
    }
    root_fd: int | None = None
    try:
        root_fd, _root = _open_salvage_root()
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        with _audit_lock:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_NONBLOCK", 0)
            fd = os.open("actions.jsonl", flags, 0o600, dir_fd=root_fd)
            try:
                audit_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(audit_stat.st_mode)
                    or audit_stat.st_nlink != 1
                    or audit_stat.st_uid != os.geteuid()
                ):
                    raise OSError("audit log is not a private regular file")
                encoded = line.encode("utf-8")
                offset = 0
                while offset < len(encoded):
                    written = os.write(fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("short audit-log write")
                    offset += written
                final_audit_stat = os.fstat(fd)
                if (
                    (final_audit_stat.st_dev, final_audit_stat.st_ino)
                    != (audit_stat.st_dev, audit_stat.st_ino)
                    or not stat.S_ISREG(final_audit_stat.st_mode)
                    or final_audit_stat.st_nlink != 1
                    or final_audit_stat.st_uid != os.geteuid()
                ):
                    raise OSError("audit log identity changed during append")
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        log(f"[SALVAGE] Audit log failure: {exc}")
    except SalvageError as exc:
        log(f"[SALVAGE] Audit log rejected unsafe path: {exc}")
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _trim_history_locked() -> None:
    if len(_jobs) <= _MAX_JOB_HISTORY:
        return
    terminal = [
        job for job in _jobs.values()
        if job["status"] in {"completed", "failed", "cancelled"}
    ]
    terminal.sort(key=lambda item: item["created_at"])
    for job in terminal[: max(0, len(_jobs) - _MAX_JOB_HISTORY)]:
        _jobs.pop(job["id"], None)


def list_jobs() -> list[dict[str, Any]]:
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda item: item["created_at"], reverse=True)
        return [_public_job(job) for job in jobs]


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return _public_job(job) if job is not None else None


def _same_exact_video_id(first: OpenedMedia, second: OpenedMedia) -> bool:
    first_id = _media_video_id(first)
    second_id = _media_video_id(second)
    return bool(first_id and first_id == second_id)


def start_job(
    source_path: object,
    mode: str,
    *,
    candidate_path: object = None,
    expected_duration_seconds: float | None = None,
    expected_source_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Validate, reserve, and start a bounded background job."""
    if mode not in {"diagnose", "remux", "reencode"}:
        return None, "mode must be diagnose, remux, or reencode", 400
    opened: list[OpenedMedia] = []
    try:
        opened.append(_open_media(source_path))
        _media_video_id(opened[-1])
        if candidate_path is not None:
            if mode != "diagnose":
                raise SalvageError("candidate_path is accepted only for diagnosis")
            opened.append(_open_media(candidate_path))
            _media_video_id(opened[-1])
            if opened[0].identity == opened[1].identity:
                raise SalvageError("Candidate resolves to the same file as the source")
            if not _same_exact_video_id(opened[0], opened[1]):
                raise SalvageError("Candidate must have the exact same case-sensitive video ID")
    except SalvageError as exc:
        for media in opened:
            media.close()
        return None, str(exc), 400

    identities = {media.identity for media in opened}
    job_id = secrets.token_hex(12)
    with LIBRARY_MUTATION_LOCK:
        try:
            # Close the open->reserve gap against every application writer.
            # These checks are cheap; the library lock is released before any
            # probe, hash, decode, or transcode work begins.
            for media in opened:
                _assert_source_unchanged(media)
                _assert_current_path_same(media)
        except SalvageError as exc:
            for media in opened:
                media.close()
            return None, str(exc), 409

        with _jobs_lock:
            active_count = sum(
                1 for job in _jobs.values() if job["status"] in {"queued", "running"}
            )
            if active_count >= MAX_CONCURRENT_SALVAGE_JOBS:
                for media in opened:
                    media.close()
                return None, "The media salvage worker is busy", 429
            if identities & _active_identities:
                for media in opened:
                    media.close()
                return None, "A salvage job is already using this file", 409

            protected_paths = [
                path
                for media in opened
                for path in (media.requested_path, media.resolved_path)
            ]
            try:
                reservation = reserve_media(
                    identities,
                    paths=protected_paths,
                    owner=f"media salvage job {job_id}",
                )
            except MediaReservationConflict as exc:
                for media in opened:
                    media.close()
                return None, str(exc), 409

            created = _now()
            job: dict[str, Any] = {
                "id": job_id,
                "kind": "diagnose" if mode == "diagnose" else "salvage",
                "mode": mode,
                "operation": mode,
                "status": "queued",
                "phase": "queued",
                "progress": 0.0,
                "progress_percent": 0.0,
                "source_path": opened[0].requested_path,
                "source_resolved_path": str(opened[0].resolved_path),
                "candidate_path": opened[1].requested_path if len(opened) > 1 else None,
                "candidate_resolved_path": str(opened[1].resolved_path) if len(opened) > 1 else None,
                "output_path": None,
                "expected_duration_seconds": expected_duration_seconds,
                "expected_source_sha256": expected_source_sha256,
                "created_at": created,
                "started_at": None,
                "completed_at": None,
                "cancel_requested": False,
                "warnings": [],
                "result": None,
                "error": None,
            }
            runtime = JobRuntime(opened, reservation=reservation)
            _jobs[job_id] = job
            _runtimes[job_id] = runtime
            _active_identities.update(identities)
            _trim_history_locked()

    _audit("started", job)
    try:
        thread = threading.Thread(
            target=_run_job,
            args=(job_id,),
            daemon=True,
            name=f"salvage-{job_id[:8]}",
        )
        thread.start()
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "failed",
                "phase": "failed",
                "completed_at": _now(),
                "error": f"Could not start salvage worker: {exc}",
            })
        _release_runtime(job_id)
        _audit("failed", job, error=str(exc))
        return None, f"Could not start salvage worker: {exc}", 500
    return get_job(job_id), None, 202


def _release_runtime(job_id: str) -> None:
    with _jobs_lock:
        runtime = _runtimes.pop(job_id, None)
        if runtime is None:
            return
        for media in runtime.sources:
            _active_identities.discard(media.identity)
    for media in runtime.sources:
        media.close()
    # Keep the lease until every held descriptor is closed.  Writers waiting
    # on the library lock may proceed immediately after this final release.
    release_media(runtime.reservation)


def _terminate_process(process: subprocess.Popen[str], *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass


def cancel_job(job_id: str) -> tuple[dict[str, Any] | None, str | None, int]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None, "Job not found", 404
        if job["status"] in {"completed", "failed", "cancelled"}:
            return _public_job(job), "Job is already finished", 409
        job["cancel_requested"] = True
        runtime = _runtimes.get(job_id)
        process = runtime.process if runtime else None
        if runtime:
            runtime.cancel.set()
        public = _public_job(job)
    if process is not None:
        _terminate_process(process)
    _audit("cancel_requested", public)
    return public, None, 202


def _read_pipe(pipe, output_queue: queue.Queue[str] | None, tail: deque[str] | None) -> None:
    try:
        while True:
            line = pipe.readline(4096)
            if not line:
                break
            line = line.rstrip("\r\n")[:_MAX_LOG_LINE_CHARS]
            if output_queue is not None:
                try:
                    output_queue.put_nowait(line)
                except queue.Full:
                    # Progress is advisory. Drop the oldest sample so a worker
                    # exiting on cancel/timeout can never strand this reader.
                    try:
                        output_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        output_queue.put_nowait(line)
                    except queue.Full:
                        pass
            elif tail is not None:
                tail.append(line)
    finally:
        pipe.close()


def _parse_time_seconds(line: str) -> float | None:
    key, separator, raw = line.partition("=")
    if not separator:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if key in {"out_time_us", "out_time_ms"}:
        return max(0.0, value / 1_000_000.0)
    if key == "out_time":
        try:
            hours, minutes, seconds = raw.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            return None
    return None


def _run_process(
    job_id: str,
    command: list[str],
    pass_fds: tuple[int, ...],
    *,
    phase: str,
    duration: float | None,
    progress_start: float,
    progress_end: float,
    output_fd: int | None = None,
    output_dir_fd: int | None = None,
    max_output_bytes: int | None = None,
) -> tuple[int, float, list[str]]:
    with _jobs_lock:
        runtime = _runtimes[job_id]
        if runtime.cancel.is_set():
            raise JobCancelled("Job cancelled")
    _update_job(job_id, phase=phase, progress=round(progress_start, 1))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=(os.name == "posix"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SalvageError(f"Could not start FFmpeg/FFprobe: {exc}") from None
    assert process.stdout is not None and process.stderr is not None
    with _jobs_lock:
        runtime = _runtimes[job_id]
        runtime.process = process

    progress_lines: queue.Queue[str] = queue.Queue(maxsize=256)
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    stdout_thread = threading.Thread(
        target=_read_pipe, args=(process.stdout, progress_lines, None), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_read_pipe, args=(process.stderr, None, stderr_tail), daemon=True
    )
    with _jobs_lock:
        deadline = _runtimes[job_id].deadline
    endpoint = 0.0
    terminated_at: float | None = None
    stdout_started = False
    stderr_started = False
    try:
        stdout_thread.start()
        stdout_started = True
        stderr_thread.start()
        stderr_started = True
        while True:
            with _jobs_lock:
                cancelled = _runtimes[job_id].cancel.is_set()
            if cancelled and process.poll() is None:
                _terminate_process(process)
                terminated_at = terminated_at or time.monotonic()
            if time.monotonic() >= deadline and process.poll() is None:
                _terminate_process(process)
                terminated_at = terminated_at or time.monotonic()
                if time.monotonic() - terminated_at >= 3:
                    _terminate_process(process, force=True)
                raise SalvageError("Media operation exceeded its configured time limit")
            if terminated_at and process.poll() is None and time.monotonic() - terminated_at >= 3:
                _terminate_process(process, force=True)
            if output_fd is not None and max_output_bytes is not None:
                try:
                    output_size = os.fstat(output_fd).st_size
                except OSError as exc:
                    raise SalvageError(f"Salvage output became unavailable: {exc}") from None
                if output_size > max_output_bytes:
                    raise SalvageError(
                        f"Salvage output exceeded its {max_output_bytes}-byte safety limit"
                    )
            if output_dir_fd is not None:
                try:
                    filesystem = os.fstatvfs(output_dir_fd)
                    free_bytes = filesystem.f_bavail * filesystem.f_frsize
                except OSError as exc:
                    raise SalvageError(f"Could not monitor salvage disk space: {exc}") from None
                if free_bytes < _OUTPUT_FREE_RESERVE_BYTES:
                    raise SalvageError("Salvage stopped to preserve free-disk reserve")

            try:
                line = progress_lines.get(timeout=0.1)
                parsed = _parse_time_seconds(line)
                if parsed is not None:
                    endpoint = max(endpoint, parsed)
                    if duration and duration > 0:
                        fraction = min(1.0, endpoint / duration)
                        progress = progress_start + (progress_end - progress_start) * fraction
                        _update_job(job_id, progress=round(progress, 1))
            except queue.Empty:
                pass
            if process.poll() is not None and not stdout_thread.is_alive() and progress_lines.empty():
                break
        returncode = process.wait(timeout=3)
    finally:
        if process.poll() is None:
            _terminate_process(process, force=True)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if stdout_started:
            stdout_thread.join(timeout=1)
        if stderr_started:
            stderr_thread.join(timeout=1)
        with _jobs_lock:
            runtime = _runtimes.get(job_id)
            if runtime and runtime.process is process:
                runtime.process = None
    with _jobs_lock:
        if _runtimes[job_id].cancel.is_set():
            raise JobCancelled("Job cancelled")
    return returncode, endpoint, list(stderr_tail)


def _probe_media(
    media: OpenedMedia,
    job_id: str | None = None,
    *,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error",
        "-protocol_whitelist", "file,pipe",
        "-format_whitelist", media.demuxer,
        "-f", media.demuxer,
        "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,codec_name,duration,width,height,sample_rate,channels:stream_disposition=attached_pic",
        "-of", "json", "-i", media.proc_path,
    ]
    timeout = max(0.1, min(60.0, timeout_seconds))
    try:
        deadline = time.monotonic() + timeout
        if job_id is not None:
            with _jobs_lock:
                runtime = _runtimes[job_id]
                remaining = runtime.deadline - time.monotonic()
                if remaining <= 0:
                    raise SalvageError("Media operation exceeded its configured time limit")
            deadline = min(deadline, runtime.deadline)
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                pass_fds=(media.fd,),
                start_new_session=(os.name == "posix"),
            )
            if job_id is not None:
                with _jobs_lock:
                    _runtimes[job_id].process = process
            try:
                while process.poll() is None:
                    if os.fstat(stdout_file.fileno()).st_size > 1024 * 1024:
                        raise SalvageError("ffprobe output exceeded its safety limit")
                    if os.fstat(stderr_file.fileno()).st_size > 64 * 1024:
                        raise SalvageError("ffprobe error output exceeded its safety limit")
                    if job_id is not None:
                        with _jobs_lock:
                            cancelled = _runtimes[job_id].cancel.is_set()
                        if cancelled:
                            _terminate_process(process)
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                _terminate_process(process, force=True)
                                process.wait(timeout=3)
                            raise JobCancelled("Job cancelled")
                    if time.monotonic() >= deadline:
                        _terminate_process(process)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            _terminate_process(process, force=True)
                            process.wait(timeout=3)
                        raise SalvageError("ffprobe exceeded its time limit")
                    time.sleep(0.05)
                returncode = process.wait(timeout=3)
            finally:
                if process.poll() is None:
                    _terminate_process(process, force=True)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                if job_id is not None:
                    with _jobs_lock:
                        runtime = _runtimes.get(job_id)
                        if runtime and runtime.process is process:
                            runtime.process = None
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_bytes = stdout_file.read(1024 * 1024 + 1)
            stderr_bytes = stderr_file.read(64 * 1024 + 1)
            if len(stdout_bytes) > 1024 * 1024 or len(stderr_bytes) > 64 * 1024:
                raise SalvageError("ffprobe output exceeded its safety limit")
            result = subprocess.CompletedProcess(
                command,
                returncode,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
            )
    except JobCancelled:
        raise
    except SalvageError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise SalvageError(f"ffprobe failed: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or "ffprobe could not read the container").strip()[:2000]
        if "moov atom not found" in detail.lower() and media.suffix in {".mp4", ".mov", ".m4v"}:
            detail += "; FFmpeg cannot recover a normal MP4 whose moov metadata is missing"
        raise SalvageError(detail)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        raise SalvageError("ffprobe returned invalid JSON") from None
    streams: list[dict[str, Any]] = []
    for raw in payload.get("streams") or []:
        if not isinstance(raw, dict) or raw.get("codec_type") not in {"video", "audio"}:
            continue
        stream: dict[str, Any] = {
            "index": int(raw.get("index", 0)),
            "type": raw.get("codec_type"),
            "codec": raw.get("codec_name"),
            "duration_seconds": _float_or_none(raw.get("duration")),
            "attached_pic": bool(
                isinstance(raw.get("disposition"), dict)
                and raw["disposition"].get("attached_pic") == 1
            ),
        }
        for name in ("width", "height", "sample_rate", "channels"):
            if raw.get(name) is not None:
                stream[name] = raw[name]
        streams.append(stream)
    if not streams:
        raise SalvageError("No video or audio streams were found")
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    return {
        "format": format_data.get("format_name"),
        "declared_duration_seconds": _float_or_none(format_data.get("duration")),
        "streams": streams,
    }


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _hash_media(job_id: str, media: OpenedMedia, start: float, end: float, phase: str) -> str:
    _update_job(job_id, phase=phase, progress=start)
    digest = hashlib.sha256()
    offset = 0
    while offset < media.size:
        with _jobs_lock:
            runtime = _runtimes[job_id]
            if runtime.cancel.is_set():
                raise JobCancelled("Job cancelled")
            if time.monotonic() >= runtime.deadline:
                raise SalvageError("Media operation exceeded its configured time limit")
        chunk = os.pread(media.fd, min(1024 * 1024, media.size - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
        _update_job(job_id, progress=round(start + (end - start) * offset / media.size, 1))
    if offset != media.size:
        raise SalvageError("Media source changed or became unreadable while hashing")
    return digest.hexdigest()


def _assert_source_unchanged(media: OpenedMedia) -> None:
    """Reject output derived from a source changed during the operation."""
    try:
        current = os.fstat(media.fd)
    except OSError as exc:
        raise SalvageError(f"Media source became unavailable: {exc}") from None
    identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    expected = (
        media.identity[0],
        media.identity[1],
        media.size,
        media.mtime_ns,
        media.ctime_ns,
    )
    if identity != expected:
        raise SalvageError("Media source changed during the operation")


def _assert_current_path_same(media: OpenedMedia) -> None:
    """Ensure the user-visible entry still resolves to the held source inode."""
    current = resolve_media_source(media.requested_path)
    if current is None:
        raise SalvageError("Media source path no longer resolves")
    try:
        canonical = current.resolve(strict=True)
        current_stat = canonical.stat()
    except (OSError, RuntimeError, ValueError):
        raise SalvageError("Media source path changed or disappeared") from None
    if not path_within(canonical, MEDIA_ROOTS):
        raise SalvageError("Media source path escaped allowed directories")
    if (current_stat.st_dev, current_stat.st_ino) != media.identity:
        raise SalvageError("Media source path was replaced during the operation")


def _decode_report(
    job_id: str,
    media: OpenedMedia,
    probe: dict[str, Any],
    *,
    progress_start: float,
    progress_end: float,
    phase_prefix: str,
) -> dict[str, Any]:
    primary_videos = [
        stream for stream in probe["streams"]
        if stream["type"] == "video" and not stream.get("attached_pic")
    ]
    audio_streams = [stream for stream in probe["streams"] if stream["type"] == "audio"]
    # A cover image is not a playable video stream. Diagnosis decodes every
    # primary video program plus every audio track. Automatic salvage remains
    # blocked when there is more than one primary video because choosing one
    # silently would discard a potentially distinct program.
    streams = primary_videos + audio_streams
    if not streams:
        raise SalvageError("No primary video or audio streams were found")
    if len(streams) > _MAX_STREAMS_TO_DECODE:
        raise SalvageError(f"Refusing media with more than {_MAX_STREAMS_TO_DECODE} audio/video streams")
    declared = probe.get("declared_duration_seconds")
    reports: list[dict[str, Any]] = []
    total = max(1, len(streams))
    for position, stream in enumerate(streams):
        span_start = progress_start + (progress_end - progress_start) * position / total
        span_end = progress_start + (progress_end - progress_start) * (position + 1) / total
        command = [
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "warning",
            "-protocol_whitelist", "file,pipe",
            "-format_whitelist", media.demuxer,
            "-f", media.demuxer,
            "-err_detect", "explode",
            "-i", media.proc_path,
            "-map", f"0:{stream['index']}",
            "-max_error_rate", "0",
            "-progress", "pipe:1", "-nostats",
            "-f", "null", "-",
        ]
        returncode, endpoint, messages = _run_process(
            job_id,
            command,
            (media.fd,),
            phase=f"{phase_prefix}_{stream['type']}_{stream['index']}",
            duration=stream.get("duration_seconds") or declared,
            progress_start=span_start,
            progress_end=span_end,
        )
        concerning = [
            line for line in messages
            if any(marker in line.lower() for marker in _ERROR_MARKERS)
        ]
        retained_messages: list[str] = []
        for line in reversed(concerning or messages):
            if line not in retained_messages:
                retained_messages.append(line)
            if len(retained_messages) >= 3:
                break
        retained_messages.reverse()
        reports.append({
            "index": stream["index"],
            "type": stream["type"],
            "returncode": returncode,
            "decoded_duration_seconds": round(endpoint, 3),
            "declared_duration_seconds": stream.get("duration_seconds"),
            "clean": returncode == 0 and not concerning,
            "messages": retained_messages,
        })
    primary_video_report = next(
        (item for item in reports if item["type"] == "video"),
        None,
    )
    if primary_video_report is not None:
        readable = primary_video_report["decoded_duration_seconds"]
    else:
        first_audio = next(
            (item for item in reports if item["type"] == "audio"),
            None,
        )
        readable = (
            first_audio["decoded_duration_seconds"] if first_audio else 0.0
        )
    return {
        "streams": reports,
        "readable_through_seconds": readable,
        "selection_warnings": [],
    }


def _materially_short(actual: float | None, expected: float | None) -> bool:
    if actual is None or expected is None:
        return False
    tolerance = max(3.0, expected * 0.02)
    return actual + tolerance < expected


def _endpoint_divergence_warnings(decode: dict[str, Any]) -> list[str]:
    """Report materially different decoded video/audio endpoints.

    Stream duration metadata is frequently absent on damaged media. Comparing
    the endpoints observed by strict decoding catches a short audio track even
    when there is no per-stream duration to compare it with. A short track can
    also be intentional, so this is deliberately a warning rather than proof
    of corruption.
    """
    streams = decode.get("streams")
    if not isinstance(streams, list):
        return []
    primary = next(
        (
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("type") == "video"
        ),
        None,
    )
    if primary is None:
        return []
    video_endpoint = _float_or_none(primary.get("decoded_duration_seconds"))
    if video_endpoint is None:
        return []
    warnings: list[str] = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("type") != "audio":
            continue
        audio_endpoint = _float_or_none(stream.get("decoded_duration_seconds"))
        if audio_endpoint is None:
            continue
        reference = max(video_endpoint, audio_endpoint)
        tolerance = max(3.0, reference * 0.02)
        if abs(video_endpoint - audio_endpoint) <= tolerance:
            continue
        warnings.append(
            f"Audio stream {stream.get('index')} and the primary video have "
            f"materially different decoded endpoints ({audio_endpoint:.3f}s "
            f"versus {video_endpoint:.3f}s). This can indicate damage or a "
            "possible intentional short track."
        )
    return warnings


def _salvage_support(probe: dict[str, Any]) -> tuple[bool, list[str]]:
    primary_videos = [
        stream for stream in probe.get("streams", [])
        if isinstance(stream, dict)
        and stream.get("type") == "video"
        and not stream.get("attached_pic")
    ]
    blockers: list[str] = []
    if len(primary_videos) > 1:
        blockers.append(
            "Multiple primary video streams require manual selection; "
            "automatic salvage is disabled."
        )
    return not blockers, blockers


def _media_report(
    job_id: str,
    media: OpenedMedia,
    *,
    expected: float | None,
    hash_start: float,
    hash_end: float,
    decode_start: float,
    decode_end: float,
    label: str,
) -> dict[str, Any]:
    checksum = _hash_media(job_id, media, hash_start, hash_end, f"hashing_{label}")
    probe = _probe_media(media, job_id)
    decode = _decode_report(
        job_id,
        media,
        probe,
        progress_start=decode_start,
        progress_end=decode_end,
        phase_prefix=f"decoding_{label}",
    )
    _assert_source_unchanged(media)
    # Re-read after decoding so the reported digest is a trustworthy baseline,
    # not a digest of bytes that changed while FFmpeg was inspecting them.
    verified_checksum = _hash_media(
        job_id, media, decode_end, decode_end, f"verifying_{label}"
    )
    if verified_checksum != checksum:
        raise SalvageError("Media source bytes changed during diagnosis")
    video_id = _media_video_id(media)
    warnings: list[str] = []
    warnings.extend(decode.get("selection_warnings") or [])
    warnings.extend(_endpoint_divergence_warnings(decode))
    if any(not stream["clean"] for stream in decode["streams"]):
        warnings.append("At least one audio/video stream did not fully decode cleanly.")
    readable = decode["readable_through_seconds"]
    declared = probe.get("declared_duration_seconds")
    for position, stream_report in enumerate(decode["streams"]):
        comparison_duration = stream_report.get("declared_duration_seconds")
        if comparison_duration is None and position == 0:
            comparison_duration = declared
        if _materially_short(
            stream_report.get("decoded_duration_seconds"), comparison_duration
        ):
            warnings.append(
                f"The {stream_report['type']} stream {stream_report['index']} ends materially before its declared duration."
            )
    if _materially_short(readable, expected):
        warnings.append("Decoded media ends materially before the supplied expected duration.")
    if readable <= 0:
        warnings.append("No positive decoded endpoint was reported; completeness is inconclusive.")
    salvage_supported, salvage_blockers = _salvage_support(probe)
    return {
        "video_id": video_id,
        "path": media.requested_path,
        "resolved_path": str(media.resolved_path),
        "size_bytes": media.size,
        "sha256": checksum,
        **probe,
        "duration_seconds": probe.get("declared_duration_seconds"),
        "decode": decode,
        "readable_duration_seconds": readable,
        "valid": not warnings,
        "warnings": warnings,
        "salvage_supported": salvage_supported,
        "salvage_blockers": salvage_blockers,
    }


def _diagnosis_result(job_id: str, runtime: JobRuntime, expected: float | None) -> dict[str, Any]:
    count = len(runtime.sources)
    source = _media_report(
        job_id,
        runtime.sources[0],
        expected=expected,
        hash_start=0,
        hash_end=5 / count,
        decode_start=5 / count,
        decode_end=100 / count,
        label="source",
    )
    candidate = None
    if count == 2:
        candidate = _media_report(
            job_id,
            runtime.sources[1],
            expected=expected,
            hash_start=50,
            hash_end=52.5,
            decode_start=52.5,
            decode_end=100,
            label="candidate",
        )
    for media, report in zip(
        runtime.sources,
        [source] + ([candidate] if candidate else []),
        strict=True,
    ):
        _assert_source_unchanged(media)
        _assert_current_path_same(media)
        if _hash_media(job_id, media, 100, 100, "final_verification") != report["sha256"]:
            raise SalvageError("Media bytes changed before diagnosis completed")
    salvage_supported = bool(source.get("salvage_supported", True))
    salvage_blockers = list(source.get("salvage_blockers") or [])
    warnings = [*source["warnings"], *salvage_blockers]
    if candidate:
        warnings.extend(f"Candidate: {message}" for message in candidate["warnings"])
    if source["warnings"]:
        assessment = "damaged"
    elif salvage_blockers or (candidate and candidate["warnings"]):
        assessment = "warning"
    else:
        assessment = "healthy"
    return {
        "assessment": assessment,
        "input_sha256": source["sha256"],
        "source": source,
        "candidate": candidate,
        "candidates": [candidate] if candidate else [],
        "expected_duration_seconds": expected,
        "salvage_supported": salvage_supported,
        "salvage_blockers": salvage_blockers,
        "findings": warnings,
        "recommendation": (
            "Automatic salvage is disabled for this stream layout; keep the "
            "original and use preview plus manual stream selection."
            if not salvage_supported else
            "Keep the original and preview it; try remux first if corruption is reported."
            if warnings else
            "Keep the original; the strict decode completed cleanly, but preview remains the final human check."
        ),
        "warnings": warnings,
        "limitations": [
            "A clean decode checks container/codec integrity, not visual or audible authenticity.",
            "The SHA-256 establishes a baseline from this scan; without an older checksum, silent historical bit changes cannot be proven retroactively.",
            "No candidate is automatically merged with or substituted for the source.",
        ],
    }


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor or ".")
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _open_salvage_root() -> tuple[int, Path]:
    """Create and pin the configured output root without following symlinks."""
    if _has_symlink_component(SALVAGE_DIR):
        raise SalvageError("Salvage output path contains a symlinked directory")
    try:
        SALVAGE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise SalvageError(f"Could not prepare salvage directory: {exc}") from None
    if _has_symlink_component(SALVAGE_DIR):
        raise SalvageError("Salvage output path contains a symlinked directory")
    try:
        root = SALVAGE_DIR.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SalvageError(f"Could not prepare salvage directory: {exc}") from None
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(SALVAGE_DIR, directory_flags)
        opened_root = Path(f"/proc/self/fd/{root_fd}").resolve(strict=True)
        root_stat = os.fstat(root_fd)
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            os.close(root_fd)
        except (OSError, UnboundLocalError):
            pass
        raise SalvageError(f"Could not pin salvage output root: {exc}") from None
    if not stat.S_ISDIR(root_stat.st_mode) or opened_root != root:
        os.close(root_fd)
        raise SalvageError("Salvage output root changed during validation")
    return root_fd, root


def _safe_output_paths(
    job_id: str,
    source: OpenedMedia,
    mode: str,
) -> tuple[int, int, Path, Path, int]:
    video_id = _media_video_id(source) or "unidentified"
    root_fd, root = _open_salvage_root()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            os.mkdir(video_id, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            job_dir_fd = os.open(video_id, directory_flags, dir_fd=root_fd)
        except OSError as exc:
            raise SalvageError(f"Could not pin salvage job directory: {exc}") from None
    finally:
        os.close(root_fd)
    try:
        job_stat = os.fstat(job_dir_fd)
        if not stat.S_ISDIR(job_stat.st_mode):
            raise SalvageError("Salvage job path is not a directory")
        resolved_dir = Path(f"/proc/self/fd/{job_dir_fd}").resolve(strict=True)
        if not path_within(resolved_dir, (root,)):
            raise SalvageError("Unsafe salvage output directory")
        free_bytes = shutil.disk_usage(resolved_dir).free
        disk_output_cap = free_bytes - _OUTPUT_FREE_RESERVE_BYTES
        if disk_output_cap <= 0:
            raise SalvageError("Insufficient space after preserving the free-disk reserve")
        final_name = (
            f"{mode}-{job_id} [{video_id}].mkv"
            if is_valid_youtube_id(video_id)
            else f"unidentified-{mode}-{job_id}.mkv"
        )
        partial_name = f".{job_id}.partial.mkv"
        try:
            os.stat(final_name, dir_fd=job_dir_fd, follow_symlinks=False)
            raise SalvageError("Salvage output collision")
        except FileNotFoundError:
            pass
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            output_fd = os.open(partial_name, flags, 0o600, dir_fd=job_dir_fd)
        except OSError as exc:
            raise SalvageError(f"Could not create private salvage output: {exc}") from None
        return (
            output_fd,
            job_dir_fd,
            resolved_dir / partial_name,
            resolved_dir / final_name,
            disk_output_cap,
        )
    except Exception:
        os.close(job_dir_fd)
        raise


def _build_salvage_command(
    source: OpenedMedia,
    output_fd: int,
    mode: str,
    probe: dict[str, Any],
    max_output_bytes: int | None = None,
) -> list[str]:
    primary_videos = [
        stream for stream in probe["streams"]
        if stream["type"] == "video" and not stream.get("attached_pic")
    ]
    audio_streams = [
        stream for stream in probe["streams"] if stream["type"] == "audio"
    ][:8]
    selected = primary_videos[:1] + audio_streams
    if len(primary_videos) > 1:
        raise SalvageError(
            "Multiple primary video streams require manual selection; automatic salvage is refused"
        )
    if not selected:
        raise SalvageError("No primary video or audio stream is available to salvage")
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "warning",
        "-protocol_whitelist", "file,pipe",
        "-format_whitelist", source.demuxer,
        "-fflags", "+discardcorrupt+genpts",
        "-err_detect", "ignore_err",
        "-f", source.demuxer,
        "-i", source.proc_path,
    ]
    for stream in selected:
        command.extend(["-map", f"0:{stream['index']}"])
    command.extend(["-map_metadata", "0", "-map_chapters", "0"])
    if mode == "remux":
        command.extend(["-c", "copy"])
    else:
        if primary_videos:
            command.extend([
                "-c:v", "libx264", "-crf", "20", "-preset", "medium",
            ])
        if audio_streams:
            command.extend([
                "-c:a", "aac", "-b:a", "192k",
                "-af", "aresample=async=1:first_pts=0",
            ])
        command.extend(["-max_muxing_queue_size", "4096"])
    command.extend([
        "-avoid_negative_ts", "make_zero",
        "-progress", "pipe:1", "-nostats", "-y",
    ])
    if max_output_bytes is not None:
        command.extend(["-fs", str(max_output_bytes)])
    command.extend(["-f", "matroska", f"/proc/self/fd/{output_fd}"])
    return command


def _opened_output(path: Path, pinned_fd: int) -> OpenedMedia:
    try:
        fd = os.dup(pinned_fd)
        os.set_inheritable(fd, False)
        file_stat = os.fstat(fd)
        opened_path = Path(f"/proc/self/fd/{fd}").resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise SalvageError(f"Could not pin generated output: {exc}") from None
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
        os.close(fd)
        raise SalvageError("Generated output is not a non-empty regular file")
    return OpenedMedia(
        requested_path=str(path),
        resolved_path=opened_path,
        fd=fd,
        identity=(file_stat.st_dev, file_stat.st_ino),
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
        demuxer="matroska",
        suffix=".mkv",
    )


def _salvage_result(
    job_id: str,
    runtime: JobRuntime,
    mode: str,
    expected: float | None,
    expected_source_sha256: str | None,
) -> dict[str, Any]:
    source = runtime.sources[0]
    input_hash = _hash_media(job_id, source, 0, 5, "hashing_source")
    if expected_source_sha256 and input_hash != expected_source_sha256:
        raise SalvageError(
            "Source checksum no longer matches the completed diagnosis; diagnose it again"
        )
    source_probe = _probe_media(source, job_id)
    duration = source_probe.get("declared_duration_seconds") or expected
    with LIBRARY_MUTATION_LOCK:
        output_fd, job_dir_fd, partial_path, final_path, disk_output_cap = _safe_output_paths(
            job_id, source, mode
        )
        try:
            if runtime.reservation is None:
                raise SalvageError("Media salvage input reservation is unavailable")
            runtime.reservation = extend_media_reservation(
                runtime.reservation,
                (media_identity(os.fstat(output_fd)),),
                paths=(partial_path,),
            )
        except Exception:
            try:
                os.unlink(partial_path.name, dir_fd=job_dir_fd)
            except OSError:
                pass
            os.close(output_fd)
            os.close(job_dir_fd)
            raise
    max_output_bytes = min(
        SALVAGE_MAX_OUTPUT_BYTES,
        max(64 * 1024 * 1024, source.size * 3),
        disk_output_cap,
    )
    runtime.partial_path = partial_path
    output_report: dict[str, Any] | None = None
    published = False
    linked = False
    output: OpenedMedia | None = None
    publish_warnings: list[str] = []
    try:
        command = _build_salvage_command(
            source, output_fd, mode, source_probe, max_output_bytes
        )
        returncode, _endpoint, messages = _run_process(
            job_id,
            command,
            (source.fd, output_fd),
            phase=mode,
            duration=duration,
            progress_start=5,
            progress_end=65,
            output_fd=output_fd,
            output_dir_fd=job_dir_fd,
            max_output_bytes=max_output_bytes,
        )
        os.fsync(output_fd)
        if returncode != 0:
            detail = messages[-1] if messages else "FFmpeg could not produce salvage output"
            raise SalvageError(detail)
        output_stat = os.fstat(output_fd)
        output_size = output_stat.st_size
        if output_size <= 0:
            raise SalvageError("FFmpeg produced an empty salvage output")
        if output_size > max_output_bytes:
            raise SalvageError("Generated output exceeded its safety size limit")
        output = _opened_output(partial_path, output_fd)
        output_report = _media_report(
            job_id,
            output,
            expected=expected,
            hash_start=95,
            hash_end=100,
            decode_start=65,
            decode_end=95,
            label="output",
        )
        if any(not item["clean"] for item in output_report["decode"]["streams"]):
            raise SalvageError("Generated output failed strict post-decode validation")
        _assert_source_unchanged(source)
        verified_input_hash = _hash_media(
            job_id, source, 100, 100, "verifying_source_unchanged"
        )
        if verified_input_hash != input_hash:
            raise SalvageError("Source bytes changed while salvage was running")
        _assert_current_path_same(source)
        _assert_source_unchanged(output)
        verified_output_hash = _hash_media(
            job_id, output, 100, 100, "verifying_output_unchanged"
        )
        if verified_output_hash != output_report["sha256"]:
            raise SalvageError("Generated output changed before publication")
        # Hashing a large result can take a long time. Revalidate both held
        # inodes and the user-visible source path after that final read, with
        # no lengthy operation left before publication.
        _assert_source_unchanged(output)
        _assert_source_unchanged(source)
        _assert_current_path_same(source)
        output_stat = os.fstat(output_fd)
        expected_output_stat = (
            output.identity[0],
            output.identity[1],
            output.size,
            output.mtime_ns,
            output.ctime_ns,
        )
        if (
            output_stat.st_dev,
            output_stat.st_ino,
            output_stat.st_size,
            output_stat.st_mtime_ns,
            output_stat.st_ctime_ns,
        ) != expected_output_stat:
            raise SalvageError("Generated output changed before publication")
        current_dir = Path(f"/proc/self/fd/{job_dir_fd}").resolve(strict=True)
        current_root = SALVAGE_DIR.resolve(strict=True)
        if not path_within(current_dir, (current_root,)):
            raise SalvageError("Output directory moved outside salvage root")
        final_path = current_dir / final_path.name
        # Publish the inode referenced by the pinned descriptor into the pinned
        # job directory. Neither source nor destination lookup trusts a mutable
        # pathname, and link(2) refuses an existing destination.
        os.link(
            f"/proc/self/fd/{output_fd}",
            final_path.name,
            dst_dir_fd=job_dir_fd,
            follow_symlinks=True,
        )
        linked = True
        final_stat = os.stat(
            final_path.name,
            dir_fd=job_dir_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or (final_stat.st_dev, final_stat.st_ino)
            != (output_stat.st_dev, output_stat.st_ino)
        ):
            raise SalvageError("Published output identity validation failed")
        published = True
        try:
            os.fsync(output_fd)
            os.fsync(job_dir_fd)
        except OSError as exc:
            publish_warnings.append(
                f"Output was published, but filesystem sync reported: {exc}"
            )
        try:
            partial_stat = os.stat(
                partial_path.name,
                dir_fd=job_dir_fd,
                follow_symlinks=False,
            )
            if (partial_stat.st_dev, partial_stat.st_ino) == (
                output_stat.st_dev,
                output_stat.st_ino,
            ):
                os.unlink(partial_path.name, dir_fd=job_dir_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            publish_warnings.append(
                f"Output was published, but partial cleanup reported: {exc}"
            )
        runtime.partial_path = None
        output_report["path"] = str(final_path)
        output_report["resolved_path"] = str(final_path)
    finally:
        if output is not None:
            output.close()
        if linked and not published:
            try:
                final_stat = os.stat(
                    final_path.name,
                    dir_fd=job_dir_fd,
                    follow_symlinks=False,
                )
                pinned_stat = os.fstat(output_fd)
                if (final_stat.st_dev, final_stat.st_ino) == (
                    pinned_stat.st_dev,
                    pinned_stat.st_ino,
                ):
                    os.unlink(final_path.name, dir_fd=job_dir_fd)
            except (FileNotFoundError, OSError):
                pass
        if not published:
            try:
                partial_stat = os.stat(
                    partial_path.name,
                    dir_fd=job_dir_fd,
                    follow_symlinks=False,
                )
                pinned_stat = os.fstat(output_fd)
                if (partial_stat.st_dev, partial_stat.st_ino) == (
                    pinned_stat.st_dev,
                    pinned_stat.st_ino,
                ):
                    os.unlink(partial_path.name, dir_fd=job_dir_fd)
            except (FileNotFoundError, OSError):
                pass
        os.close(output_fd)
        os.close(job_dir_fd)
        runtime.partial_path = None

    assert output_report is not None
    warnings = [*output_report["warnings"], *publish_warnings]
    declared = output_report.get("declared_duration_seconds")
    if _materially_short(declared, source_probe.get("declared_duration_seconds")):
        warnings.append("Salvage output is materially shorter than the source container duration.")
    outcome = "salvaged_with_warnings" if warnings else "salvaged"
    return {
        "outcome": outcome,
        "input_sha256": input_hash,
        "output_sha256": output_report["sha256"],
        "source": {
            "video_id": _media_video_id(source),
            "path": source.requested_path,
            "size_bytes": source.size,
            **source_probe,
        },
        "output": output_report,
        "warnings": warnings,
        "limitations": [
            "Best-effort salvage preserves only packets/frames FFmpeg can still read; missing bytes cannot be reconstructed.",
            "The original file was opened read-only and was not modified.",
        ],
    }


def _compact_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    def media_summary(media: object) -> dict[str, Any] | None:
        if not isinstance(media, dict):
            return None
        decode = media.get("decode") if isinstance(media.get("decode"), dict) else {}
        return {
            "path": media.get("path"),
            "video_id": media.get("video_id"),
            "size_bytes": media.get("size_bytes"),
            "declared_duration_seconds": media.get("declared_duration_seconds"),
            "readable_through_seconds": decode.get("readable_through_seconds"),
            "sha256": media.get("sha256"),
            "warnings": list(media.get("warnings") or [])[:10],
        }

    return {
        "assessment": result.get("assessment"),
        "outcome": result.get("outcome"),
        "input_sha256": result.get("input_sha256"),
        "output_sha256": result.get("output_sha256"),
        "warnings": list(result.get("warnings") or [])[:20],
        "source": media_summary(result.get("source")),
        "candidate": media_summary(result.get("candidate")),
        "output": media_summary(result.get("output")),
    }


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        runtime = _runtimes[job_id]
        job.update({"status": "running", "phase": "starting", "started_at": _now()})
        mode = job["mode"]
        expected = job["expected_duration_seconds"]
        expected_source_sha256 = job["expected_source_sha256"]
    try:
        if runtime.cancel.is_set():
            raise JobCancelled("Job cancelled")
        if mode == "diagnose":
            result = _diagnosis_result(job_id, runtime, expected)
            output_path = None
        else:
            result = _salvage_result(
                job_id,
                runtime,
                mode,
                expected,
                expected_source_sha256,
            )
            output_path = result["output"]["path"]
        with _jobs_lock:
            job = _jobs[job_id]
            completed_values = {
                "status": "completed",
                "phase": "complete",
                "progress": 100.0,
                "progress_percent": 100.0,
                "completed_at": _now(),
                "output_path": output_path,
                "warnings": result.get("warnings", []),
                "result": result,
            }
            if mode == "diagnose":
                completed_values["diagnosis"] = result
            else:
                completed_values["output_duration_seconds"] = (
                    result.get("output", {}).get("declared_duration_seconds")
                )
            job.update(completed_values)
            public = _public_job(job)
        _audit(
            "completed",
            public,
            outcome=result.get("outcome") or result.get("assessment"),
            input_sha256=result.get("input_sha256"),
            output_sha256=result.get("output_sha256"),
            result_summary=_compact_result_summary(result),
        )
    except JobCancelled:
        with _jobs_lock:
            job = _jobs[job_id]
            job.update({
                "status": "cancelled", "phase": "cancelled",
                "completed_at": _now(), "error": None,
            })
            public = _public_job(job)
        _audit("cancelled", public)
    except Exception as exc:  # worker boundary must always release files/slots
        message = str(exc)[:4000] or exc.__class__.__name__
        with _jobs_lock:
            job = _jobs[job_id]
            job.update({
                "status": "failed", "phase": "failed",
                "completed_at": _now(), "error": message,
            })
            public = _public_job(job)
        log(f"[SALVAGE] Job {job_id} failed: {message}")
        _audit("failed", public, error=message)
    finally:
        # `_salvage_result` owns descriptor-checked partial cleanup. Never
        # unlink a mutable path blindly from this generic worker boundary.
        _release_runtime(job_id)


def _quick_probe_path(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_video_id: str,
    timeout_seconds: float = 10.0,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        media = _open_media(str(path))
    except SalvageError as exc:
        if str(exc).startswith((
            "Unsupported media container",
            "Media source is empty",
            "Media source is not a regular file",
        )):
            return None, str(exc)
        return None, f"{_CANDIDATE_SNAPSHOT_ERROR_PREFIX} or became unavailable: {exc}"
    try:
        if media.identity != expected_identity:
            return None, f"{_CANDIDATE_SNAPSHOT_ERROR_PREFIX} after enumeration"
        try:
            opened_video_id = _media_video_id(media)
        except SalvageError as exc:
            return None, f"{_CANDIDATE_SNAPSHOT_ERROR_PREFIX}: {exc}"
        if opened_video_id != expected_video_id:
            return None, (
                f"{_CANDIDATE_SNAPSHOT_ERROR_PREFIX}: the opened entry no "
                "longer has the exact case-sensitive video ID"
            )
        try:
            probe = _probe_media(media, timeout_seconds=timeout_seconds)
        except SalvageError as exc:
            return None, str(exc)
        try:
            _assert_source_unchanged(media)
            _assert_current_path_same(media)
        except SalvageError as exc:
            return None, f"{_CANDIDATE_SNAPSHOT_ERROR_PREFIX} while probing: {exc}"
        return {
            "resolved_path": str(media.resolved_path),
            "size_bytes": media.size,
            "duration_seconds": probe.get("declared_duration_seconds"),
            "streams": probe["streams"],
            "format": probe.get("format"),
        }, None
    finally:
        media.close()


def _exact_id_candidate_inventory(
    video_id: str,
    *,
    exclude_path: str | None = None,
) -> dict[str, Any]:
    """Find only case-sensitive exact-ID files/targets; never fuzzy-match."""
    if not is_valid_youtube_id(video_id):
        raise SalvageError("video_id must be an exact 11-character YouTube ID")
    excluded_identity: tuple[int, int] | None = None
    if exclude_path:
        excluded = _open_media(exclude_path)
        try:
            _media_video_id(excluded)
            excluded_identity = excluded.identity
        finally:
            excluded.close()
    entries: list[CandidateScanEntry] = []
    identities: set[tuple[int, int]] = set()
    scanned = 0
    deadline = time.monotonic() + _CANDIDATE_BUDGET_SECONDS
    for root in _CANDIDATE_ROOTS:
        if (
            not root.exists()
            or len(entries) >= _MAX_CANDIDATES
            or scanned >= _MAX_CANDIDATE_SCAN_ENTRIES
            or time.monotonic() >= deadline
        ):
            continue
        try:
            iterator = root.rglob("*")
            for entry in iterator:
                scanned += 1
                if (
                    len(entries) >= _MAX_CANDIDATES
                    or scanned >= _MAX_CANDIDATE_SCAN_ENTRIES
                    or time.monotonic() >= deadline
                ):
                    break
                if not (entry.is_file() or entry.is_symlink()):
                    continue
                resolved = resolve_media_source(entry)
                if resolved is None:
                    continue
                try:
                    canonical = resolved.resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    continue
                try:
                    candidate_video_id = _video_id_from_path_evidence(entry, canonical)
                except SalvageError:
                    # Contradictory entry/target IDs are never exact-match
                    # candidates for either ID.
                    continue
                if candidate_video_id != video_id:
                    continue
                if not path_within(canonical, MEDIA_ROOTS):
                    continue
                try:
                    candidate_stat = canonical.stat()
                except OSError:
                    continue
                identity = (candidate_stat.st_dev, candidate_stat.st_ino)
                if identity == excluded_identity or identity in identities:
                    continue
                identities.add(identity)
                entries.append(CandidateScanEntry(
                    path=entry,
                    enumerated_resolved_path=canonical,
                    entry_type="symlink" if entry.is_symlink() else "file",
                    expected_identity=identity,
                    expected_video_id=video_id,
                ))
        except OSError:
            continue

    candidates: list[dict[str, Any]] = []
    snapshot_invalidated = False
    for entry in entries:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        probe, error = _quick_probe_path(
            entry.path,
            expected_identity=entry.expected_identity,
            expected_video_id=entry.expected_video_id,
            timeout_seconds=min(5.0, remaining),
        )
        if error and error.startswith(_CANDIDATE_SNAPSHOT_ERROR_PREFIX):
            snapshot_invalidated = True
        candidates.append({
            "path": str(entry.path),
            "enumerated_resolved_path": str(entry.enumerated_resolved_path),
            "entry_type": entry.entry_type,
            "exact_id": probe is not None,
            "exact_video_id_match": probe is not None,
            "valid": probe is not None,
            "probe_error": error,
            **(probe or {}),
        })
    complete = (
        not snapshot_invalidated
        and time.monotonic() < deadline
        and scanned < _MAX_CANDIDATE_SCAN_ENTRIES
        and len(entries) < _MAX_CANDIDATES
    )
    if complete:
        message = "Exact-ID candidate scan completed."
    elif snapshot_invalidated:
        message = "A candidate changed during validation; rescan before relying on this inventory."
    elif len(entries) >= _MAX_CANDIDATES:
        message = f"Candidate scan stopped at the {_MAX_CANDIDATES}-file safety limit."
    elif scanned >= _MAX_CANDIDATE_SCAN_ENTRIES:
        message = "Candidate scan stopped at the filesystem-entry safety limit."
    else:
        message = "Candidate scan stopped at the wall-time safety limit."
    return {
        "candidates": candidates,
        "scan_complete": complete,
        "truncated": not complete,
        "scan_message": message,
        "scanned_entries": scanned,
    }


def exact_id_candidate_inventory(
    video_id: str,
    *,
    exclude_path: str | None = None,
) -> dict[str, Any]:
    if not _candidate_scan_guard.acquire(blocking=False):
        raise SalvageError("An exact-ID candidate scan is already running")
    try:
        return _exact_id_candidate_inventory(
            video_id,
            exclude_path=exclude_path,
        )
    finally:
        _candidate_scan_guard.release()


def exact_id_candidates(
    video_id: str,
    *,
    exclude_path: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility helper returning only candidates from the bounded inventory."""
    return exact_id_candidate_inventory(
        video_id,
        exclude_path=exclude_path,
    )["candidates"]


def reset_state_for_tests() -> None:
    """Clear completed in-memory state; tests must not call with live workers."""
    runtimes: list[JobRuntime]
    with _jobs_lock:
        runtimes = list(_runtimes.values())
        for runtime in runtimes:
            runtime.cancel.set()
            if runtime.process:
                _terminate_process(runtime.process, force=True)
        _jobs.clear()
        _runtimes.clear()
        _active_identities.clear()
    for runtime in runtimes:
        for source in runtime.sources:
            source.close()
        release_media(runtime.reservation)
