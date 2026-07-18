"""Durable, guarded UNTRUNC repairs for recovered-channel media.

UNTRUNC always writes a private candidate under ``SALVAGE_DIR``.  Replacing a
source file is a separate, checksum-bound action that first creates and
verifies a permanent backup.  Target/Hidden trees are never mutated here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import (
    SALVAGE_DIR,
    SOURCE_DIR,
    UNTRUNC_BINARY,
    UNTRUNC_INSPECTION_TIMEOUT_SECONDS,
    UNTRUNC_JOB_TIMEOUT_SECONDS,
    UNTRUNC_MAX_OUTPUT_BYTES,
)
from .db import get_db
from . import salvage
from .logging_util import log
from .media_reservations import (
    MediaReservation,
    MediaReservationConflict,
    media_identity,
    media_mutation_guard,
    release_media,
    reserve_media,
)
from .utils import path_within


SUPPORTED_SUFFIXES = {".mp4", ".mov", ".m4v", ".3gp"}
_DEMUXERS = {".mp4": "mov", ".mov": "mov", ".m4v": "mov", ".3gp": "mov"}
_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "adopted", "restored"}
_ACTIVE = {"queued", "running", "cancelling", "adopting", "restoring"}
_HASH_CHUNK = 1024 * 1024
_LOG_LIMIT = 80
_LOG_LINE_LIMIT = 1000
_FREE_RESERVE = 64 * 1024 * 1024
_TOKEN_TTL = 10 * 60


def _subprocess_env() -> dict[str, str]:
    """Give native media parsers only the locale and executable search path."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


class ReferenceRepairError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class RepairCancelled(ReferenceRepairError):
    pass


class _Runtime:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.process: subprocess.Popen[Any] | None = None
        self.reservation: MediaReservation | None = None


_lock = threading.RLock()
_runtimes: dict[str, _Runtime] = {}
_threads: dict[str, threading.Thread] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _decode(value: object, default: Any = None) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    size = os.fstat(fd).st_size
    while offset < size:
        chunk = os.pread(fd, min(_HASH_CHUNK, size - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if offset != size:
        raise ReferenceRepairError("Media ended before its recorded size", 409)
    return digest.hexdigest()


def _has_symlink_below(root: Path, path: Path) -> bool:
    """Reject mutable directory indirection below a configured mount root."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return True
    return False


def _fingerprint(st: os.stat_result) -> dict[str, int]:
    return {
        "device": st.st_dev,
        "inode": st.st_ino,
        "size_bytes": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
    }


def _fingerprint_matches(row: Any, st: os.stat_result) -> bool:
    return (
        row["device"] == st.st_dev
        and row["inode"] == st.st_ino
        and row["size_bytes"] == st.st_size
        and row["mtime_ns"] == st.st_mtime_ns
        and row["ctime_ns"] == st.st_ctime_ns
    )


def _case_and_item(conn: Any, case_id: str, item_id: str) -> tuple[Any, Any]:
    case = conn.execute(
        "SELECT * FROM recovered_channel_cases WHERE case_id = ?", (case_id,)
    ).fetchone()
    if case is None:
        raise ReferenceRepairError("Recovered channel case not found", 404)
    item = conn.execute(
        "SELECT * FROM recovered_channel_items WHERE case_id = ? AND item_id = ?",
        (case_id, item_id),
    ).fetchone()
    if item is None:
        raise ReferenceRepairError("Recovered channel item not found", 404)
    return case, item


def _open_item(case: Any, row: Any) -> tuple[int, Path, os.stat_result]:
    path = Path(row["source_path"])
    root = SOURCE_DIR / case["source_channel_id"]
    try:
        path.relative_to(root)
    except ValueError:
        raise ReferenceRepairError("Media path escaped its recovered channel", 409) from None
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ReferenceRepairError("UNTRUNC supports MP4, MOV, M4V, and 3GP inputs only", 409)
    if _has_symlink_below(SOURCE_DIR, path):
        raise ReferenceRepairError("Media path contains a symlinked directory", 409)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        st = os.fstat(fd)
        opened = Path(f"/proc/self/fd/{fd}").resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise ReferenceRepairError(f"Media could not be opened safely: {exc}", 409) from None
    try:
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise ReferenceRepairError("Media must be a non-empty regular file", 409)
        if not path_within(opened, (root,)):
            raise ReferenceRepairError("Opened media escaped its recovered channel", 409)
        if not _fingerprint_matches(row, st):
            raise ReferenceRepairError("Media changed since the recovered inventory was recorded", 409)
        return fd, path, st
    except Exception:
        os.close(fd)
        raise


def _diagnosis_streams(row: Any) -> list[dict[str, Any]]:
    diagnosis = _decode(row["diagnosis_json"], {})
    source = diagnosis.get("source") if isinstance(diagnosis, dict) else None
    streams = source.get("streams") if isinstance(source, dict) else None
    return [item for item in streams or [] if isinstance(item, dict)]


def _stream_key(stream: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        stream.get(name)
        for name in (
            "type", "codec", "profile", "pixel_format", "width", "height",
            "frame_rate", "time_base", "sample_rate", "channels", "channel_layout",
        )
    )


def ranked_references(case_id: str, damaged_item_id: str) -> dict[str, Any]:
    with get_db() as conn:
        case, damaged = _case_and_item(conn, case_id, damaged_item_id)
        if damaged["health_tier"] not in {"serious", "inconclusive"}:
            raise ReferenceRepairError("Only Needs closer review media can be repaired", 409)
        if Path(damaged["source_path"]).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ReferenceRepairError("This damaged container is not supported by UNTRUNC", 409)
        candidates = conn.execute(
            """
            SELECT * FROM recovered_channel_items
            WHERE case_id = ? AND item_id != ? AND video_id != ?
              AND inspection_status = 'completed'
              AND health_tier IN ('clear', 'minimal')
            ORDER BY video_id, source_path
            """,
            (case_id, damaged_item_id, damaged["video_id"]),
        ).fetchall()
    damaged_streams = _diagnosis_streams(damaged)
    damaged_suffix = Path(damaged["source_path"]).suffix.lower()
    results: list[dict[str, Any]] = []
    for row in candidates:
        suffix = Path(row["source_path"]).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            continue
        streams = _diagnosis_streams(row)
        score = 0
        reasons: list[str] = []
        if suffix == damaged_suffix:
            score += 30
            reasons.append("same container suffix")
        if damaged_streams and streams:
            damaged_keys = [_stream_key(stream) for stream in damaged_streams]
            reference_keys = [_stream_key(stream) for stream in streams]
            if damaged_keys == reference_keys:
                score += 70
                reasons.append("matching stream layout and encoding characteristics")
            else:
                damaged_codecs = [(item.get("type"), item.get("codec")) for item in damaged_streams]
                reference_codecs = [(item.get("type"), item.get("codec")) for item in streams]
                if damaged_codecs == reference_codecs:
                    score += 35
                    reasons.append("matching codec layout")
        results.append({
            "item_id": row["item_id"],
            "video_id": row["video_id"],
            "filename": Path(row["source_path"]).name,
            "size_bytes": row["size_bytes"],
            "health_tier": row["health_tier"],
            "score": score,
            "compatibility": (
                "uncertain" if not damaged_streams else
                "strong" if score >= 80 else "possible" if score >= 45 else "weak"
            ),
            "reasons": reasons or ["same recovered channel; format match is uncertain"],
            "streams": streams,
            "preview_url": "/api/recovery/preview?filepath=" + quote(row["source_path"], safe=""),
            "preview_audio_url": "/api/recovery/preview-audio?filepath=" + quote(row["source_path"], safe=""),
        })
    results.sort(key=lambda item: (-item["score"], item["video_id"], item["item_id"]))
    total = len(results)
    results = results[:200]
    return {
        "damaged_item_id": damaged_item_id,
        "compatibility_uncertain": not bool(damaged_streams),
        "total": total,
        "truncated": total > len(results),
        "references": results,
    }


def _public(row: Any) -> dict[str, Any]:
    before = _decode(row["before_json"], None)
    after = _decode(row["after_json"], None)
    logs = _decode(row["log_json"], [])
    candidate_path = row["candidate_path"]
    backup_path = row["backup_path"]
    return {
        "repair_id": row["repair_id"], "case_id": row["case_id"],
        "damaged_item_id": row["damaged_item_id"],
        "reference_item_id": row["reference_item_id"],
        "reference_video_id": (
            row["reference_video_id"] if "reference_video_id" in row.keys() else None
        ),
        "status": row["status"], "phase": row["phase"],
        "progress": row["progress"], "assessment": row["assessment"],
        "decision": row["decision"], "source_sha256": row["source_sha256"],
        "reference_sha256": row["reference_sha256"],
        "candidate_sha256": row["candidate_sha256"],
        "before": before, "after": after, "logs": logs,
        "error": row["error"], "created_by": row["created_by"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "completed_at": row["completed_at"], "adopted_at": row["adopted_at"],
        "restored_at": row["restored_at"], "has_backup": bool(row["backup_path"]),
        "tool_version": row["tool_version"],
        "action_actor": row["action_actor"],
        "candidate_preview_url": (
            "/api/recovery/preview?filepath=" + quote(candidate_path, safe="")
            if candidate_path else None
        ),
        "candidate_audio_url": (
            "/api/recovery/preview-audio?filepath=" + quote(candidate_path, safe="")
            if candidate_path else None
        ),
        "backup_preview_url": (
            "/api/recovery/preview?filepath=" + quote(backup_path, safe="")
            if backup_path else None
        ),
        "backup_audio_url": (
            "/api/recovery/preview-audio?filepath=" + quote(backup_path, safe="")
            if backup_path else None
        ),
    }


def get_repair(case_id: str, repair_id: str) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT recovered_media_repairs.*, reference.video_id AS reference_video_id
               FROM recovered_media_repairs
               LEFT JOIN recovered_channel_items AS reference
                 ON reference.item_id = recovered_media_repairs.reference_item_id
               WHERE recovered_media_repairs.case_id = ?
                 AND recovered_media_repairs.repair_id = ?""",
            (case_id, repair_id),
        ).fetchone()
    if row is None:
        raise ReferenceRepairError("Reference repair not found", 404)
    return _public(row)


def list_repairs(case_id: str, damaged_item_id: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        query = """SELECT recovered_media_repairs.*, reference.video_id AS reference_video_id
                   FROM recovered_media_repairs
                   LEFT JOIN recovered_channel_items AS reference
                     ON reference.item_id = recovered_media_repairs.reference_item_id
                   WHERE recovered_media_repairs.case_id = ?"""
        values: list[Any] = [case_id]
        if damaged_item_id:
            query += " AND recovered_media_repairs.damaged_item_id = ?"
            values.append(damaged_item_id)
        query += " ORDER BY recovered_media_repairs.created_at DESC, recovered_media_repairs.repair_id DESC"
        rows = conn.execute(query, values).fetchall()
    return [_public(row) for row in rows]


def _event(conn: Any, case_id: str, event: str, *, actor: str | None = None,
           item_id: str | None = None, details: object = None) -> None:
    conn.execute(
        "INSERT INTO recovered_channel_events (case_id,item_id,actor,event,details_json) VALUES (?,?,?,?,?)",
        (case_id, item_id, actor, event, _json(details) if details is not None else None),
    )


def start_repair(case_id: str, damaged_item_id: str, reference_item_id: str,
                 *, actor: str) -> dict[str, Any]:
    if (not isinstance(damaged_item_id, str) or not damaged_item_id
            or not isinstance(reference_item_id, str) or not reference_item_id):
        raise ReferenceRepairError(
            "damaged_item_id and reference_item_id must be database item IDs"
        )
    if damaged_item_id == reference_item_id:
        raise ReferenceRepairError("Damaged media cannot be its own reference")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case, damaged = _case_and_item(conn, case_id, damaged_item_id)
        _case2, reference = _case_and_item(conn, case_id, reference_item_id)
        if (damaged["inspection_status"] != "completed"
                or damaged["health_tier"] not in {"serious", "inconclusive"}):
            raise ReferenceRepairError("Damaged media is not in Needs closer review", 409)
        if not isinstance(damaged["sha256"], str):
            raise ReferenceRepairError("Damaged media is missing its inspection checksum", 409)
        if reference["inspection_status"] != "completed" or reference["health_tier"] not in {"clear", "minimal"}:
            raise ReferenceRepairError("Reference must be a completed Likely usable video", 409)
        if not isinstance(reference["sha256"], str):
            raise ReferenceRepairError("Reference is missing its inspection checksum", 409)
        if reference["video_id"] == damaged["video_id"]:
            raise ReferenceRepairError(
                "An exact-ID alternate copy belongs in reconciliation, not structural reference repair",
                409,
            )
        if conn.execute(
            "SELECT 1 FROM recovered_media_repairs WHERE status IN ('queued','running','cancelling','adopting','restoring')"
        ).fetchone():
            raise ReferenceRepairError("Another UNTRUNC repair is already active", 409)
        if conn.execute(
            "SELECT 1 FROM recovered_channel_jobs WHERE case_id = ? AND status IN ('queued','running','cancelling')",
            (case_id,),
        ).fetchone():
            raise ReferenceRepairError("This recovered channel already has active work", 409)
        repair_id = secrets.token_hex(12)
        conn.execute(
            """
            INSERT INTO recovered_media_repairs
                (repair_id,case_id,damaged_item_id,reference_item_id,status,phase,created_by)
            VALUES (?,?,?,?, 'queued','queued',?)
            """,
            (repair_id, case_id, damaged_item_id, reference_item_id, actor),
        )
        _event(conn, case_id, "reference_repair_started", actor=actor,
               item_id=damaged_item_id,
               details={"repair_id": repair_id, "reference_item_id": reference_item_id})
        conn.commit()
    _launch(repair_id)
    return get_repair(case_id, repair_id)


def retry_repair(case_id: str, repair_id: str, *, actor: str) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM recovered_media_repairs WHERE case_id=? AND repair_id=?",
            (case_id, repair_id),
        ).fetchone()
    if row is None:
        raise ReferenceRepairError("Reference repair not found", 404)
    if row["status"] not in {"failed", "cancelled", "interrupted"}:
        raise ReferenceRepairError("Only failed, cancelled, or interrupted repairs can be retried", 409)
    return start_repair(case_id, row["damaged_item_id"], row["reference_item_id"], actor=actor)


def cancel_repair(case_id: str, repair_id: str, *, actor: str) -> dict[str, Any]:
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM recovered_media_repairs WHERE case_id=? AND repair_id=?",
            (case_id, repair_id),
        ).fetchone()
        if row is None:
            raise ReferenceRepairError("Reference repair not found", 404)
        if row["status"] not in {"queued", "running", "cancelling"}:
            raise ReferenceRepairError("Reference repair is not active", 409)
        cursor = conn.execute(
            "UPDATE recovered_media_repairs SET cancel_requested=1,status='cancelling',phase='cancelling' WHERE repair_id=? AND status IN ('queued','running','cancelling')",
            (repair_id,),
        )
        if cursor.rowcount != 1:
            raise ReferenceRepairError("Reference repair state changed before cancellation", 409)
        _event(conn, case_id, "reference_repair_cancel_requested", actor=actor,
               item_id=row["damaged_item_id"], details={"repair_id": repair_id})
        conn.commit()
    with _lock:
        runtime = _runtimes.get(repair_id)
        if runtime:
            runtime.cancel.set()
            if runtime.process and runtime.process.poll() is None:
                try:
                    os.killpg(runtime.process.pid, signal.SIGTERM)
                except OSError:
                    pass
    return get_repair(case_id, repair_id)


def _prepare_dir(repair_id: str) -> Path:
    salvage_root_fd, salvage_root = salvage._open_salvage_root()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    repairs_fd = job_fd = -1
    try:
        try:
            os.mkdir("reference-repairs", mode=0o700, dir_fd=salvage_root_fd)
        except FileExistsError:
            pass
        repairs_fd = os.open("reference-repairs", directory_flags, dir_fd=salvage_root_fd)
        if not stat.S_ISDIR(os.fstat(repairs_fd).st_mode):
            raise ReferenceRepairError("Reference-repair root is not a directory", 500)
        try:
            os.mkdir(repair_id, mode=0o700, dir_fd=repairs_fd)
        except FileExistsError:
            raise ReferenceRepairError("Reference-repair directory collision", 409) from None
        job_fd = os.open(repair_id, directory_flags, dir_fd=repairs_fd)
        job = Path(f"/proc/self/fd/{job_fd}").resolve(strict=True)
        repairs_root = Path(f"/proc/self/fd/{repairs_fd}").resolve(strict=True)
        if job.parent != repairs_root or not path_within(job, (salvage_root,)):
            raise ReferenceRepairError("Unsafe reference-repair job directory", 500)
        return job
    finally:
        if job_fd >= 0: os.close(job_fd)
        if repairs_fd >= 0: os.close(repairs_fd)
        os.close(salvage_root_fd)


def _run_capture(command: list[str], *, pass_fds: tuple[int, ...], timeout: float,
                 cancel: threading.Event | None = None) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            pass_fds=pass_fds, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise RepairCancelled("Repair cancelled during media inspection")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                for stream in (stdout, stderr):
                    if os.fstat(stream.fileno()).st_size > 1024 * 1024:
                        stream.seek(0)
                        stream.truncate(0)
                time.sleep(0.05)
            returncode = process.wait(timeout=3)
        except (subprocess.TimeoutExpired, RepairCancelled) as exc:
            try: os.killpg(process.pid, signal.SIGTERM)
            except OSError: pass
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except OSError: pass
                process.wait(timeout=3)
            if isinstance(exc, RepairCancelled):
                raise
            raise ReferenceRepairError("Media inspection exceeded its time limit", 504) from None
        stdout.seek(0); stderr.seek(0)
        return subprocess.CompletedProcess(
            command, returncode, stdout.read(1024 * 1024), stderr.read(1024 * 1024)
        )


def _bounded_lines(stdout: bytes, stderr: bytes) -> list[str]:
    lines: list[str] = []
    for raw in (stdout + b"\n" + stderr).decode("utf-8", errors="replace").splitlines():
        value = raw.strip()
        if value:
            lines.append(value[:_LOG_LINE_LIMIT])
    return lines[-_LOG_LIMIT:]


def _decoded_endpoint(progress: bytes) -> float | None:
    endpoint: float | None = None
    for raw in progress.decode("utf-8", errors="replace").splitlines():
        if not raw.startswith("out_time="):
            continue
        value = raw.partition("=")[2].strip()
        try:
            hours, minutes, seconds = value.split(":", 2)
            candidate = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            continue
        if math.isfinite(candidate) and candidate >= 0:
            endpoint = max(endpoint or 0, candidate)
    return endpoint


def _inspect_fd(fd: int, suffix: str, *, timeout: float = 300,
                cancel: threading.Event | None = None) -> dict[str, Any]:
    proc_path = f"/proc/self/fd/{fd}"
    entries = (
        "format=format_name,format_long_name,duration,size,format_name:"
        "stream=index,codec_type,codec_name,profile,pix_fmt,width,height,avg_frame_rate,time_base,"
        "sample_rate,channels,channel_layout,duration:stream_disposition=attached_pic"
    )
    probe = _run_capture(
        ["ffprobe", "-v", "error", "-f", _DEMUXERS[suffix], "-show_entries", entries,
         "-of", "json", "-i", proc_path],
        pass_fds=(fd,), timeout=min(timeout, 60), cancel=cancel,
    )
    base: dict[str, Any] = {"sha256": _hash_fd(fd), "size_bytes": os.fstat(fd).st_size}
    if probe.returncode != 0:
        base.update({"probeable": False, "health_tier": "inconclusive", "streams": [],
                     "warnings": _bounded_lines(probe.stdout, probe.stderr)})
        return base
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError:
        base.update({"probeable": False, "health_tier": "inconclusive", "streams": [],
                     "warnings": ["ffprobe returned invalid JSON"]})
        return base
    streams: list[dict[str, Any]] = []
    for raw in payload.get("streams") or []:
        if raw.get("codec_type") not in {"video", "audio"}:
            continue
        streams.append({
            "index": raw.get("index"), "type": raw.get("codec_type"),
            "codec": raw.get("codec_name"), "profile": raw.get("profile"),
            "pixel_format": raw.get("pix_fmt"), "width": raw.get("width"),
            "height": raw.get("height"), "frame_rate": raw.get("avg_frame_rate"),
            "time_base": raw.get("time_base"), "sample_rate": raw.get("sample_rate"),
            "channels": raw.get("channels"), "channel_layout": raw.get("channel_layout"),
            "duration_seconds": _float(raw.get("duration")),
            "attached_pic": bool((raw.get("disposition") or {}).get("attached_pic")),
        })
    if not streams:
        base.update({"probeable": False, "health_tier": "inconclusive", "streams": [],
                     "warnings": ["No video or audio streams were found"]})
        return base
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _float(format_data.get("duration"))
    decode = _run_capture(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", _DEMUXERS[suffix],
         "-i", proc_path, "-map", "0:v?", "-map", "0:a?", "-progress", "pipe:1",
         "-nostats", "-f", "null", "-"],
        pass_fds=(fd,), timeout=timeout, cancel=cancel,
    )
    warnings = _bounded_lines(b"", decode.stderr)
    seekable: bool | None = None
    if duration and duration > 1:
        seek = _run_capture(
            ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss",
             str(max(0.0, duration * 0.9)), "-f", _DEMUXERS[suffix], "-i", proc_path,
             "-t", "0.25", "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"],
            pass_fds=(fd,), timeout=min(timeout, 30), cancel=cancel,
        )
        seekable = seek.returncode == 0
        if not seekable:
            warnings.extend(_bounded_lines(seek.stdout, seek.stderr))
    base.update({
        "probeable": True, "format": format_data.get("format_name"),
        "format_long_name": format_data.get("format_long_name"),
        "duration_seconds": duration, "decoded_endpoint_seconds": _decoded_endpoint(decode.stdout),
        "seekable": seekable, "streams": streams,
        "decode_clean": decode.returncode == 0, "decode_returncode": decode.returncode,
        "warnings": warnings, "errors": warnings,
        "health_tier": "clear" if decode.returncode == 0 and not warnings else "serious",
    })
    return base


def _float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _compare(before: dict[str, Any], after: dict[str, Any]) -> str:
    if not after.get("probeable"):
        return "worse"
    if not before.get("probeable"):
        return "better"
    before_types = [item.get("type") for item in before.get("streams") or [] if not item.get("attached_pic")]
    after_types = [item.get("type") for item in after.get("streams") or [] if not item.get("attached_pic")]
    if any(kind not in after_types for kind in before_types):
        return "worse"
    if not before.get("decode_clean") and after.get("decode_clean"):
        return "better"
    if before.get("decode_clean") and not after.get("decode_clean"):
        return "worse"
    before_duration = before.get("duration_seconds")
    after_duration = after.get("duration_seconds")
    if isinstance(before_duration, (int, float)) and isinstance(after_duration, (int, float)):
        tolerance = max(2.0, before_duration * 0.02)
        if after_duration > before_duration + tolerance:
            return "better"
        if after_duration + tolerance < before_duration:
            return "worse"
    before_warnings = len(before.get("warnings") or [])
    after_warnings = len(after.get("warnings") or [])
    if after_warnings < before_warnings:
        return "better"
    if after_warnings > before_warnings:
        return "worse"
    return "uncertain"


def _run_worker(repair_id: str) -> None:
    runtime = _runtimes[repair_id]
    damaged_fd = reference_fd = -1
    partial: Path | None = None
    reference_link: Path | None = None
    damaged_link: Path | None = None
    final: Path | None = None
    completed = False
    try:
        tool_version = _tool_version()
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM recovered_media_repairs WHERE repair_id=?", (repair_id,)).fetchone()
            if row is None:
                return
            if row["cancel_requested"] or row["status"] == "cancelling":
                raise RepairCancelled("Repair cancelled")
            if row["status"] != "queued":
                return
            case, damaged = _case_and_item(conn, row["case_id"], row["damaged_item_id"])
            _case2, reference = _case_and_item(conn, row["case_id"], row["reference_item_id"])
            cursor = conn.execute(
                """UPDATE recovered_media_repairs
                   SET status='running',phase='validating',progress=1,started_at=?,tool_version=?
                   WHERE repair_id=? AND status='queued' AND cancel_requested=0""",
                (_now(), tool_version, repair_id),
            )
            if cursor.rowcount != 1:
                raise RepairCancelled("Repair cancelled before it started")
            conn.commit()
        damaged_fd, damaged_path, damaged_stat = _open_item(case, damaged)
        reference_fd, reference_path, reference_stat = _open_item(case, reference)
        if media_identity(damaged_stat) == media_identity(reference_stat):
            raise ReferenceRepairError("Damaged media and reference resolve to the same file", 409)
        if damaged_stat.st_nlink != 1:
            raise ReferenceRepairError(
                "Damaged source has hard-link aliases; replacement is blocked", 409
            )
        runtime.reservation = reserve_media(
            (media_identity(damaged_stat), media_identity(reference_stat)),
            paths=(damaged_path, reference_path), owner=f"UNTRUNC repair {repair_id}",
        )
        damaged_hash = _hash_fd(damaged_fd)
        reference_hash = _hash_fd(reference_fd)
        if damaged_hash != damaged["sha256"]:
            raise ReferenceRepairError("Damaged bytes changed since completed inspection", 409)
        if reference_hash != reference["sha256"]:
            raise ReferenceRepairError("Reference bytes changed since its completed inspection", 409)
        # Always normalize a fresh before-report. Older salvage diagnoses do
        # not carry the probeable/decode fields used by comparison.
        before_source = _inspect_fd(
            damaged_fd, damaged_path.suffix.lower(),
            timeout=UNTRUNC_INSPECTION_TIMEOUT_SECONDS, cancel=runtime.cancel,
        )
        job_dir = _prepare_dir(repair_id)
        suffix = damaged_path.suffix.lower()
        partial = job_dir / f".candidate.partial{suffix}"
        final = job_dir / f"candidate{suffix}"
        free = shutil.disk_usage(job_dir).free - _FREE_RESERVE
        max_output = min(UNTRUNC_MAX_OUTPUT_BYTES, max(damaged_stat.st_size * 3, 64 * 1024 * 1024), free)
        if max_output <= 0:
            raise ReferenceRepairError("Insufficient storage for a repair candidate", 507)
        # Upstream derives media details from filename suffixes. These private
        # links resolve through inherited child descriptors and never expose a
        # mutable archive pathname to UNTRUNC.
        reference_link = job_dir / f"reference{reference_path.suffix.lower()}"
        damaged_link = job_dir / f"damaged{damaged_path.suffix.lower()}"
        os.symlink(f"/proc/self/fd/{reference_fd}", reference_link)
        os.symlink(f"/proc/self/fd/{damaged_fd}", damaged_link)
        command = [UNTRUNC_BINARY, "-n", "-dst", str(partial),
                   str(reference_link), str(damaged_link)]
        with get_db() as conn:
            conn.execute(
                "UPDATE recovered_media_repairs SET phase='repairing',progress=10,source_sha256=?,reference_sha256=?,source_fingerprint_json=?,reference_fingerprint_json=?,before_json=? WHERE repair_id=?",
                (damaged_hash, reference_hash, _json(_fingerprint(damaged_stat)),
                 _json(_fingerprint(reference_stat)), _json(before_source), repair_id),
            )
            conn.commit()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    pass_fds=(damaged_fd, reference_fd), start_new_session=True,
                    cwd=str(job_dir), env=_subprocess_env(),
                )
            except OSError as exc:
                raise ReferenceRepairError(f"UNTRUNC could not start: {exc}", 503) from None
            runtime.process = process
            deadline = time.monotonic() + UNTRUNC_JOB_TIMEOUT_SECONDS
            try:
                while process.poll() is None:
                    if runtime.cancel.is_set():
                        raise RepairCancelled("Repair cancelled")
                    if time.monotonic() >= deadline:
                        raise ReferenceRepairError("UNTRUNC exceeded its configured time limit", 504)
                    try:
                        current_size = partial.stat().st_size
                    except FileNotFoundError:
                        current_size = 0
                    if current_size > max_output:
                        raise ReferenceRepairError("UNTRUNC output exceeded its safety limit", 507)
                    if shutil.disk_usage(job_dir).free < _FREE_RESERVE:
                        raise ReferenceRepairError("UNTRUNC stopped to preserve free disk space", 507)
                    for stream in (stdout, stderr):
                        if os.fstat(stream.fileno()).st_size > 1024 * 1024:
                            stream.seek(0)
                            stream.truncate(0)
                    time.sleep(0.2)
                returncode = process.wait(timeout=3)
            except Exception:
                if process.poll() is None:
                    try: os.killpg(process.pid, signal.SIGTERM)
                    except OSError: pass
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except OSError: pass
                        process.wait(timeout=3)
                raise
            runtime.process = None
            stdout.seek(0); stderr.seek(0)
            logs = _bounded_lines(stdout.read(1024 * 1024), stderr.read(1024 * 1024))
        if returncode != 0:
            raise ReferenceRepairError(logs[-1] if logs else f"UNTRUNC exited with status {returncode}", 422)
        if _hash_fd(damaged_fd) != damaged_hash:
            raise ReferenceRepairError("Damaged source changed while UNTRUNC was running", 409)
        if _hash_fd(reference_fd) != reference_hash:
            raise ReferenceRepairError("Reference changed while UNTRUNC was running", 409)
        out_stat = os.lstat(partial)
        if not stat.S_ISREG(out_stat.st_mode) or out_stat.st_size <= 0 or out_stat.st_size > max_output:
            raise ReferenceRepairError("UNTRUNC did not create a valid non-empty candidate", 422)
        os.replace(partial, final)
        output_fd = os.open(final, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(output_fd)
            after = _inspect_fd(
                output_fd, suffix, timeout=UNTRUNC_INSPECTION_TIMEOUT_SECONDS,
                cancel=runtime.cancel,
            )
        finally:
            os.close(output_fd)
        job_dir_fd = os.open(job_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(job_dir_fd)
        finally: os.close(job_dir_fd)
        reference_link.unlink(missing_ok=True)
        damaged_link.unlink(missing_ok=True)
        if not after.get("probeable"):
            final.unlink(missing_ok=True)
            raise ReferenceRepairError("UNTRUNC output contains no probeable audio or video streams", 422)
        if after["sha256"] in {damaged_hash, reference_hash}:
            final.unlink(missing_ok=True)
            raise ReferenceRepairError("UNTRUNC output is only an unchanged input copy", 422)
        assessment = _compare(before_source, after)
        if runtime.cancel.is_set():
            raise RepairCancelled("Repair cancelled")
        with get_db() as conn:
            cursor = conn.execute(
                """UPDATE recovered_media_repairs SET status='completed',phase='review',progress=100,
                   candidate_sha256=?,candidate_path=?,after_json=?,assessment=?,log_json=?,completed_at=?,error=NULL
                   WHERE repair_id=? AND status='running' AND cancel_requested=0""",
                (after["sha256"], str(final), _json(after), assessment, _json(logs), _now(), repair_id),
            )
            if cursor.rowcount != 1:
                raise RepairCancelled("Repair was cancelled before candidate publication")
            _event(conn, row["case_id"], "reference_repair_completed",
                   item_id=row["damaged_item_id"],
                   details={"repair_id": repair_id, "assessment": assessment})
            conn.commit()
        completed = True
    except RepairCancelled as exc:
        _finish_error(repair_id, "cancelled", str(exc))
    except Exception as exc:
        _finish_error(repair_id, "failed", str(exc) or exc.__class__.__name__)
    finally:
        runtime.process = None
        release_media(runtime.reservation)
        for entry in (partial, reference_link, damaged_link):
            if entry is not None:
                try: entry.unlink(missing_ok=True)
                except OSError: pass
        if not completed and final is not None:
            try: final.unlink(missing_ok=True)
            except OSError: pass
        for fd in (damaged_fd, reference_fd):
            if fd >= 0:
                try: os.close(fd)
                except OSError: pass


def _finish_error(repair_id: str, status_value: str, message: str) -> None:
    message = message[:2000]
    with get_db() as conn:
        row = conn.execute("SELECT case_id,damaged_item_id FROM recovered_media_repairs WHERE repair_id=?", (repair_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE recovered_media_repairs SET status=?,phase=?,completed_at=?,error=? WHERE repair_id=?",
            (status_value, status_value, _now(), message, repair_id),
        )
        _event(conn, row["case_id"], f"reference_repair_{status_value}",
               item_id=row["damaged_item_id"], details={"repair_id": repair_id, "error": message})
        conn.commit()
    log(f"[REFERENCE REPAIR] {repair_id} {status_value}: {message}")


def _launch(repair_id: str) -> None:
    runtime = _Runtime()
    def runner() -> None:
        try:
            _run_worker(repair_id)
        finally:
            with _lock:
                _threads.pop(repair_id, None)
                _runtimes.pop(repair_id, None)
    thread = threading.Thread(target=runner, daemon=True, name=f"reference-repair-{repair_id[:8]}")
    with _lock:
        _runtimes[repair_id] = runtime
        _threads[repair_id] = thread
    try:
        thread.start()
    except Exception as exc:
        with _lock:
            _runtimes.pop(repair_id, None); _threads.pop(repair_id, None)
        _finish_error(repair_id, "failed", f"Could not start repair worker: {exc}")
        raise ReferenceRepairError("Could not start reference repair worker", 503) from exc


def _token_payload(row: Any, action: str) -> dict[str, Any]:
    return {
        "repair_id": row["repair_id"], "action": action,
        "source_sha256": row["source_sha256"], "candidate_sha256": row["candidate_sha256"],
        "backup_sha256": row["backup_sha256"], "candidate_path": row["candidate_path"],
        "backup_path": row["backup_path"],
    }


def _file_snapshot(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    snapshot: dict[str, Any] = {
        "path": str(path), "exists": True, "mode": stat.S_IFMT(entry.st_mode),
        "device": entry.st_dev, "inode": entry.st_ino,
    }
    if stat.S_ISLNK(entry.st_mode):
        snapshot["target"] = os.readlink(path)
    return snapshot


def _tool_version() -> str:
    try:
        result = subprocess.run(
            [UNTRUNC_BINARY, "-V"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10, check=False, text=True, env=_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return (result.stdout or "unknown").strip()[:200]


def build_action_plan(case_id: str, repair_id: str, action: str, *, actor: str) -> dict[str, Any]:
    if action not in {"adopt", "restore"}:
        raise ReferenceRepairError("Unknown repair action")
    # First collect and hash immutable-looking state without holding a SQLite
    # write lock. Large archive files can live on slow network storage.
    with get_db() as conn:
        row = conn.execute("SELECT * FROM recovered_media_repairs WHERE case_id=? AND repair_id=?", (case_id, repair_id)).fetchone()
        if row is None:
            raise ReferenceRepairError("Reference repair not found", 404)
        if action == "adopt" and (row["status"] != "completed" or row["decision"] != "pending"):
            raise ReferenceRepairError("Repair is not ready for adoption", 409)
        if action == "restore" and (
            row["status"] != "adopted" or row["decision"] != "adopted" or not row["backup_path"]
        ):
            raise ReferenceRepairError("This repair has no adopted original to restore", 409)
        case, item = _case_and_item(conn, case_id, row["damaged_item_id"])
        fd, source, st = _open_item(case, item)
        try:
            current_hash = _hash_fd(fd)
        finally:
            os.close(fd)
        expected = row["source_sha256"] if action == "adopt" else row["candidate_sha256"]
        if current_hash != expected:
            raise ReferenceRepairError("Source bytes no longer match this repair action", 409)
        payload = _token_payload(row, action)
        candidate_stat = (
            os.stat(row["candidate_path"], follow_symlinks=False)
            if action == "adopt" else None
        )
        backup_stat = (
            os.stat(row["backup_path"], follow_symlinks=False)
            if row["backup_path"] and os.path.lexists(row["backup_path"]) else None
        )
        confirmation = f"{action.upper()} {item['video_id']}"
        payload.update({"source_path": str(source), "current_sha256": current_hash,
                        "source_fingerprint": _fingerprint(st),
                        "candidate_fingerprint": _fingerprint(candidate_stat) if candidate_stat else None,
                        "backup_fingerprint": _fingerprint(backup_stat) if backup_stat else None,
                        "organized_link": _file_snapshot(item["organized_path"]),
                        "tool_version": row["tool_version"], "confirmation": confirmation})
        row_snapshot = _token_payload(row, action)
        item_snapshot = {
            key: item[key] for key in (
                "source_path", "device", "inode", "size_bytes", "mtime_ns", "ctime_ns",
                "sha256", "organized_path", "inspection_status", "health_tier", "decision",
            )
        }
    token = secrets.token_urlsafe(32)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM recovered_media_repairs WHERE case_id=? AND repair_id=?",
            (case_id, repair_id),
        ).fetchone()
        if current is None or _token_payload(current, action) != row_snapshot:
            raise ReferenceRepairError("Repair state changed while preparing the dry run", 409)
        if action == "adopt" and (current["status"] != "completed" or current["decision"] != "pending"):
            raise ReferenceRepairError("Repair is not ready for adoption", 409)
        if action == "restore" and (current["status"] != "adopted" or current["decision"] != "adopted"):
            raise ReferenceRepairError("Repair is not ready for restoration", 409)
        _case_current, current_item = _case_and_item(conn, case_id, current["damaged_item_id"])
        if any(current_item[key] != value for key, value in item_snapshot.items()):
            raise ReferenceRepairError("Recovered item changed while preparing the dry run", 409)
        try:
            current_stat = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise ReferenceRepairError(f"Source changed while preparing the dry run: {exc}", 409) from None
        if _fingerprint(current_stat) != payload["source_fingerprint"]:
            raise ReferenceRepairError("Source changed while preparing the dry run", 409)
        conn.execute(
            "DELETE FROM recovered_media_repair_tokens WHERE expires_at < ? OR consumed_at IS NOT NULL",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO recovered_media_repair_tokens (token_hash,repair_id,action,actor,payload_json,expires_at) VALUES (?,?,?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), repair_id, action, actor, _json(payload), time.time() + _TOKEN_TTL),
        )
        conn.commit()
    return {
        "plan_token": token, "action": action, "repair_id": repair_id,
        "source_path": payload["source_path"], "candidate_path": row["candidate_path"],
        "backup_path": row["backup_path"] or str(Path(row["candidate_path"]).parent / f"original-backup{Path(source).suffix.lower()}"),
        "source_sha256": current_hash,
        "replacement_sha256": row["candidate_sha256"] if action == "adopt" else row["backup_sha256"],
        "target_mutations": 0, "expires_in_seconds": _TOKEN_TTL,
        "confirmation": confirmation,
    }


def _consume_token(conn: Any, repair_id: str, action: str, token: str, actor: str) -> dict[str, Any]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute("SELECT * FROM recovered_media_repair_tokens WHERE token_hash=?", (token_hash,)).fetchone()
    if row is None or row["repair_id"] != repair_id or row["action"] != action or row["actor"] != actor:
        raise ReferenceRepairError("Invalid repair confirmation token", 409)
    if row["consumed_at"] or row["expires_at"] < time.time():
        raise ReferenceRepairError("Repair confirmation token expired or was already used", 409)
    cursor = conn.execute("UPDATE recovered_media_repair_tokens SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL", (_now(), token_hash))
    if cursor.rowcount != 1:
        raise ReferenceRepairError("Repair confirmation token was already used", 409)
    return _decode(row["payload_json"], {})


def _copy_fd(source_fd: int, destination: Path, mode: int) -> tuple[str, os.stat_result]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    out_fd = os.open(destination, flags, mode)
    try:
        os.fchmod(out_fd, mode)
        offset = 0
        size = os.fstat(source_fd).st_size
        while offset < size:
            chunk = os.pread(source_fd, min(_HASH_CHUNK, size - offset), offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(out_fd, view)
                view = view[written:]
            offset += len(chunk)
        if offset != size:
            raise ReferenceRepairError("Copy ended before the recorded source size", 409)
        os.fsync(out_fd)
        checksum = _hash_fd(out_fd)
        return checksum, os.fstat(out_fd)
    finally:
        os.close(out_fd)


def _open_source_parent(path: Path) -> int:
    """Walk from SOURCE_DIR with no-follow directory descriptors."""
    root = SOURCE_DIR.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ReferenceRepairError("Source parent escaped the configured source root", 409) from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReferenceRepairError("Source path cannot be addressed safely", 409)
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def _copy_fd_at(source_fd: int, destination_name: str, parent_fd: int,
                mode: int) -> tuple[str, os.stat_result]:
    flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    out_fd = os.open(destination_name, flags, mode, dir_fd=parent_fd)
    try:
        os.fchmod(out_fd, mode)
        offset = 0
        size = os.fstat(source_fd).st_size
        while offset < size:
            chunk = os.pread(source_fd, min(_HASH_CHUNK, size - offset), offset)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(out_fd, view)
                view = view[written:]
            offset += len(chunk)
        if offset != size:
            raise ReferenceRepairError("Copy ended before the recorded replacement size", 409)
        os.fsync(out_fd)
        return _hash_fd(out_fd), os.fstat(out_fd)
    finally:
        os.close(out_fd)


def apply_action(case_id: str, repair_id: str, action: str, token: str,
                 confirmation: str, *, actor: str) -> dict[str, Any]:
    if action not in {"adopt", "restore"} or not isinstance(token, str) or not token:
        raise ReferenceRepairError("A valid action and plan_token are required")
    source_fd = replacement_fd = parent_fd = staged_fd = -1
    source: Path | None = None
    staged: Path | None = None
    staged_name: str | None = None
    partial_backup: Path | None = None
    published = False
    prior_status = "completed" if action == "adopt" else "adopted"
    prior_phase = "review" if action == "adopt" else "adopted"
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        repair = conn.execute("SELECT * FROM recovered_media_repairs WHERE case_id=? AND repair_id=?", (case_id, repair_id)).fetchone()
        if repair is None:
            raise ReferenceRepairError("Reference repair not found", 404)
        payload = _consume_token(conn, repair_id, action, token, actor)
        if confirmation != payload.get("confirmation"):
            raise ReferenceRepairError("Exact confirmation text did not match", 409)
        case, item = _case_and_item(conn, case_id, repair["damaged_item_id"])
        if conn.execute(
            "SELECT 1 FROM recovered_channel_jobs WHERE case_id=? AND status IN ('queued','running','cancelling')",
            (case_id,),
        ).fetchone():
            raise ReferenceRepairError("Recovered channel work became active after the dry run", 409)
        if conn.execute(
            """SELECT 1 FROM recovered_media_repairs
               WHERE repair_id != ?
                 AND status IN ('queued','running','cancelling','adopting','restoring')""",
            (repair_id,),
        ).fetchone():
            raise ReferenceRepairError("Another reference repair became active after the dry run", 409)
        expected_status = "completed" if action == "adopt" else "adopted"
        expected_decision = "pending" if action == "adopt" else "adopted"
        cursor = conn.execute(
            "UPDATE recovered_media_repairs SET status=?,phase=?,action_actor=? WHERE repair_id=? AND status=? AND decision=?",
            ("adopting" if action == "adopt" else "restoring", action,
             actor, repair_id, expected_status, expected_decision),
        )
        if cursor.rowcount != 1:
            raise ReferenceRepairError("Repair action state changed after the dry run", 409)
        conn.commit()
    try:
        source_fd, source, source_stat = _open_item(case, item)
        parent_fd = _open_source_parent(source)
        parent_source_stat = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if _fingerprint(parent_source_stat) != _fingerprint(source_stat):
            raise ReferenceRepairError("Source parent changed before replacement staging", 409)
        replacement_path = Path(repair["candidate_path"] if action == "adopt" else repair["backup_path"])
        expected_source_hash = repair["source_sha256"] if action == "adopt" else repair["candidate_sha256"]
        if _hash_fd(source_fd) != expected_source_hash or payload.get("current_sha256") != expected_source_hash:
            raise ReferenceRepairError("Source changed after the dry run", 409)
        replacement_fd = os.open(replacement_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        replacement_stat = os.fstat(replacement_fd)
        if not stat.S_ISREG(replacement_stat.st_mode):
            raise ReferenceRepairError("Replacement is not a regular file", 409)
        expected_replacement_hash = repair["candidate_sha256"] if action == "adopt" else repair["backup_sha256"]
        if _hash_fd(replacement_fd) != expected_replacement_hash:
            raise ReferenceRepairError("Replacement bytes no longer match the repair record", 409)
        expected_replacement_fingerprint = (
            payload.get("candidate_fingerprint") if action == "adopt"
            else payload.get("backup_fingerprint")
        )
        if _fingerprint(replacement_stat) != expected_replacement_fingerprint:
            raise ReferenceRepairError("Replacement fingerprint changed after the dry run", 409)
        if _file_snapshot(item["organized_path"]) != payload.get("organized_link"):
            raise ReferenceRepairError("Managed-link state changed after the dry run", 409)
        backup_path = Path(repair["backup_path"] or (replacement_path.parent / f"original-backup{source.suffix.lower()}"))
        if action == "adopt":
            # Journal the deterministic private backup destination before any
            # bytes are created, closing the crash window between fsync and DB.
            if not repair["backup_path"]:
                with get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute(
                        "UPDATE recovered_media_repairs SET backup_path=? WHERE repair_id=? AND status='adopting' AND backup_path IS NULL",
                        (str(backup_path), repair_id),
                    )
                    if cursor.rowcount != 1:
                        raise ReferenceRepairError("Repair backup plan changed before creation", 409)
                    conn.commit()
            backup_hash: str | None = None
            if backup_path.exists():
                backup_fd = os.open(backup_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    backup_hash = _hash_fd(backup_fd)
                    if not stat.S_ISREG(os.fstat(backup_fd).st_mode) or backup_hash != expected_source_hash:
                        raise ReferenceRepairError("Recorded original backup is no longer valid", 409)
                finally:
                    os.close(backup_fd)
            else:
                if shutil.disk_usage(backup_path.parent).free < source_stat.st_size + _FREE_RESERVE:
                    raise ReferenceRepairError("Insufficient space for the permanent original backup", 507)
                if backup_path.exists():
                    raise ReferenceRepairError("An unrecorded file occupies the backup destination", 409)
                partial_backup = backup_path.with_name(
                    f".backup-{secrets.token_hex(8)}.partial{source.suffix.lower()}"
                )
                backup_hash, _backup_stat = _copy_fd(source_fd, partial_backup, 0o600)
                if backup_hash != expected_source_hash:
                    partial_backup.unlink(missing_ok=True)
                    raise ReferenceRepairError("Original backup verification failed", 500)
                os.replace(partial_backup, backup_path)
                partial_backup = None
                backup_dir_fd = os.open(backup_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try: os.fsync(backup_dir_fd)
                finally: os.close(backup_dir_fd)
            # Commit verified backup provenance before the source can change.
            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE recovered_media_repairs SET backup_sha256=? WHERE repair_id=? AND status='adopting' AND backup_path=? AND (backup_sha256 IS NULL OR backup_sha256=?)",
                    (backup_hash, repair_id, str(backup_path), backup_hash),
                )
                if cursor.rowcount != 1:
                    raise ReferenceRepairError("Repair backup state changed before publication", 409)
                conn.commit()
        source_fs = os.fstatvfs(parent_fd)
        source_free = source_fs.f_bavail * source_fs.f_frsize
        if source_free < replacement_stat.st_size + _FREE_RESERVE:
            raise ReferenceRepairError("Insufficient source-filesystem space to stage replacement", 507)
        staged_name = f".{source.stem}.repair-{secrets.token_hex(8)}{source.suffix}"
        staged = source.parent / staged_name
        staged_hash, staged_stat = _copy_fd_at(
            replacement_fd, staged_name, parent_fd, stat.S_IMODE(source_stat.st_mode)
        )
        if staged_hash != expected_replacement_hash:
            raise ReferenceRepairError("Staged replacement verification failed", 500)
        staged_fd = os.open(
            staged_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
        )
        with media_mutation_guard(paths=(source, replacement_path), action=f"{action} this recovered repair"):
            current_stat = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
            if _fingerprint(current_stat) != _fingerprint(source_stat) or _hash_fd(source_fd) != expected_source_hash:
                raise ReferenceRepairError("Source changed immediately before replacement", 409)
            current_staged = os.stat(staged_name, dir_fd=parent_fd, follow_symlinks=False)
            if (_fingerprint(current_staged) != _fingerprint(staged_stat)
                    or _fingerprint(os.fstat(staged_fd)) != _fingerprint(staged_stat)
                    or _hash_fd(staged_fd) != expected_replacement_hash):
                raise ReferenceRepairError("Staged replacement changed before publication", 409)
            os.replace(staged_name, source.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            staged = None
            staged_name = None
            published = True
            os.fsync(parent_fd)
        new_stat = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        published_fd = os.open(
            source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
        )
        try:
            if (_fingerprint(os.fstat(published_fd)) != _fingerprint(new_stat)
                    or _hash_fd(published_fd) != expected_replacement_hash):
                raise ReferenceRepairError("Published source path changed after replacement", 409)
        finally:
            os.close(published_fd)
        report = _decode(repair["after_json"], {}) if action == "adopt" else _decode(repair["before_json"], {})
        new_hash = expected_replacement_hash
        health = report.get("health_tier") if isinstance(report, dict) else "inconclusive"
        if health not in {"clear", "minimal", "serious", "inconclusive"}:
            health = "inconclusive"
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if action == "adopt":
                repair_cursor = conn.execute(
                    "UPDATE recovered_media_repairs SET status='adopted',phase='adopted',decision='adopted',backup_path=?,backup_sha256=?,adopted_at=?,error=NULL WHERE repair_id=? AND status='adopting' AND decision='pending'",
                    (str(backup_path), expected_source_hash, _now(), repair_id),
                )
            else:
                repair_cursor = conn.execute(
                    "UPDATE recovered_media_repairs SET status='restored',phase='restored',decision='restored',restored_at=?,error=NULL WHERE repair_id=? AND status='restoring' AND decision='adopted'",
                    (_now(), repair_id),
                )
            if repair_cursor.rowcount != 1:
                raise ReferenceRepairError("Repair state changed during source publication", 409)
            item_cursor = conn.execute(
                """UPDATE recovered_channel_items SET size_bytes=?,device=?,inode=?,mtime_ns=?,ctime_ns=?,
                   sha256=?,inspection_status='completed',health_tier=?,diagnosis_json=?,
                   decision=CASE WHEN organized_path IS NULL THEN 'pending' ELSE decision END,
                   error=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE case_id=? AND item_id=? AND device=? AND inode=? AND size_bytes=?
                     AND mtime_ns=? AND ctime_ns=? AND sha256 IS ?
                     AND inspection_status IS ? AND health_tier IS ?
                     AND diagnosis_json IS ? AND decision IS ?""",
                (new_stat.st_size, new_stat.st_dev, new_stat.st_ino, new_stat.st_mtime_ns,
                 new_stat.st_ctime_ns, new_hash, health, _json({"source": report, "assessment": health}),
                 case_id, repair["damaged_item_id"], item["device"], item["inode"],
                 item["size_bytes"], item["mtime_ns"], item["ctime_ns"], item["sha256"],
                 item["inspection_status"], item["health_tier"], item["diagnosis_json"],
                 item["decision"]),
            )
            if item_cursor.rowcount != 1:
                raise ReferenceRepairError("Recovered item changed during source publication", 409)
            event_name = "reference_repair_adopted" if action == "adopt" else "reference_repair_restored"
            _event(conn, case_id, event_name, actor=actor,
                   item_id=repair["damaged_item_id"], details={"repair_id": repair_id, "source_path": str(source)})
            conn.commit()
    except Exception as exc:
        with get_db() as conn:
            current_hash = None
            if source is not None:
                try:
                    if parent_fd >= 0:
                        current_fd = os.open(
                            source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent_fd,
                        )
                    else:
                        current_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try: current_hash = _hash_fd(current_fd)
                    finally: os.close(current_fd)
                except OSError:
                    pass
            expected_source_hash = repair["source_sha256"] if action == "adopt" else repair["candidate_sha256"]
            expected_replacement_hash = repair["candidate_sha256"] if action == "adopt" else repair["backup_sha256"]
            message = (str(exc) or exc.__class__.__name__)[:2000]
            if not published and current_hash == expected_source_hash:
                conn.execute(
                    "UPDATE recovered_media_repairs SET status=?,phase=?,error=? WHERE repair_id=?",
                    (prior_status, prior_phase, message, repair_id),
                )
            elif current_hash == expected_replacement_hash:
                conn.execute(
                    "UPDATE recovered_media_repairs SET error=? WHERE repair_id=?",
                    ("Replacement reached source; restart reconciliation required: " + message, repair_id),
                )
            else:
                conn.execute(
                    "UPDATE recovered_media_repairs SET status='recovery_blocked',phase='manual_review',error=? WHERE repair_id=?",
                    (message, repair_id),
                )
            conn.commit()
        if current_hash == expected_replacement_hash:
            recover_interrupted_repairs(repair_id)
            reconciled = get_repair(case_id, repair_id)
            if reconciled["status"] in {"adopted", "restored"}:
                return {"repair": reconciled, "item_id": repair["damaged_item_id"]}
        if isinstance(exc, MediaReservationConflict):
            raise ReferenceRepairError(str(exc), 409) from None
        raise
    finally:
        if partial_backup is not None:
            try: partial_backup.unlink(missing_ok=True)
            except OSError: pass
        if staged_name is not None and parent_fd >= 0:
            try: os.unlink(staged_name, dir_fd=parent_fd)
            except FileNotFoundError: pass
        elif staged is not None:
            staged.unlink(missing_ok=True)
        if staged_fd >= 0:
            os.close(staged_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if source_fd >= 0:
            os.close(source_fd)
        if replacement_fd >= 0:
            os.close(replacement_fd)
    return {"repair": get_repair(case_id, repair_id), "item_id": repair["damaged_item_id"]}


def recover_interrupted_repairs(repair_id: str | None = None) -> None:
    """Fail closed after restart; finalize only an unambiguous adopted hash."""
    def artifact_valid(path_value: object, checksum: object) -> bool:
        if not isinstance(path_value, str) or not isinstance(checksum, str):
            return False
        try:
            fd = os.open(path_value, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                return stat.S_ISREG(os.fstat(fd).st_mode) and _hash_fd(fd) == checksum
            finally:
                os.close(fd)
        except (OSError, ReferenceRepairError):
            return False

    def finalize_item(conn: Any, item: Any, row: Any, *, restored: bool) -> None:
        path = Path(item["source_path"])
        new_stat = os.stat(path, follow_symlinks=False)
        report = _decode(row["before_json"] if restored else row["after_json"], {})
        health = report.get("health_tier", "inconclusive")
        checksum = row["backup_sha256"] if restored else row["candidate_sha256"]
        conn.execute(
            """UPDATE recovered_channel_items SET size_bytes=?,device=?,inode=?,mtime_ns=?,ctime_ns=?,
               sha256=?,inspection_status='completed',health_tier=?,diagnosis_json=?,
               decision=CASE WHEN organized_path IS NULL THEN 'pending' ELSE decision END,
               error=NULL,updated_at=CURRENT_TIMESTAMP WHERE item_id=?""",
            (new_stat.st_size,new_stat.st_dev,new_stat.st_ino,new_stat.st_mtime_ns,
             new_stat.st_ctime_ns,checksum,health,_json({"source":report,"assessment":health}),
             item["item_id"]),
        )

    with get_db() as conn:
        if repair_id is None:
            rows = conn.execute(
                "SELECT * FROM recovered_media_repairs WHERE status IN ('queued','running','cancelling','adopting','restoring')"
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM recovered_media_repairs
                   WHERE repair_id=? AND status IN ('adopting','restoring')""",
                (repair_id,),
            ).fetchall()
        for row in rows:
            if row["status"] in {"queued", "running", "cancelling"}:
                conn.execute("UPDATE recovered_media_repairs SET status='interrupted',phase='interrupted',completed_at=?,error='Server restarted before the repair completed' WHERE repair_id=?", (_now(), row["repair_id"]))
                job_dir = SALVAGE_DIR / "reference-repairs" / row["repair_id"]
                if path_within(job_dir.resolve(strict=False), (SALVAGE_DIR.resolve(strict=False),)):
                    for name in (
                        ".candidate.partial.mp4", ".candidate.partial.mov",
                        ".candidate.partial.m4v", ".candidate.partial.3gp",
                        "candidate.mp4", "candidate.mov", "candidate.m4v", "candidate.3gp",
                        "reference.mp4", "reference.mov", "reference.m4v", "reference.3gp",
                        "damaged.mp4", "damaged.mov", "damaged.m4v", "damaged.3gp",
                    ):
                        try: (job_dir / name).unlink(missing_ok=True)
                        except OSError: pass
                continue
            item = conn.execute("SELECT * FROM recovered_channel_items WHERE item_id=?", (row["damaged_item_id"],)).fetchone()
            private_job_dir = SALVAGE_DIR / "reference-repairs" / row["repair_id"]
            if path_within(
                private_job_dir.resolve(strict=False), (SALVAGE_DIR.resolve(strict=False),)
            ):
                for partial_backup in private_job_dir.glob(".backup-*.partial*"):
                    try:
                        entry = os.lstat(partial_backup)
                        if stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                            partial_backup.unlink()
                    except OSError:
                        pass
            try:
                path = Path(item["source_path"])
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try: current_hash = _hash_fd(fd)
                finally: os.close(fd)
            except (OSError, TypeError):
                current_hash = None
            candidate_valid = artifact_valid(row["candidate_path"], row["candidate_sha256"])
            backup_valid = artifact_valid(row["backup_path"], row["backup_sha256"])
            if row["status"] == "restoring" and current_hash == row["backup_sha256"]:
                conn.execute("UPDATE recovered_media_repairs SET status='restored',phase='restored',decision='restored',restored_at=?,error=NULL WHERE repair_id=?", (_now(), row["repair_id"]))
                finalize_item(conn, item, row, restored=True)
                _event(conn, row["case_id"], "reference_repair_restore_reconciled",
                       actor=row["action_actor"] or row["created_by"], item_id=row["damaged_item_id"],
                       details={"repair_id": row["repair_id"], "source_sha256": current_hash})
            elif (row["status"] == "restoring" and current_hash == row["candidate_sha256"]
                  and backup_valid):
                conn.execute("UPDATE recovered_media_repairs SET status='adopted',phase='adopted',decision='adopted',error='Restore was interrupted before source publication' WHERE repair_id=?", (row["repair_id"],))
            elif row["status"] == "restoring":
                conn.execute("UPDATE recovered_media_repairs SET status='recovery_blocked',phase='manual_review',error='Interrupted restoration could not be reconciled safely' WHERE repair_id=?", (row["repair_id"],))
                _event(conn, row["case_id"], "reference_repair_recovery_blocked",
                       actor=row["action_actor"] or row["created_by"], item_id=row["damaged_item_id"],
                       details={"repair_id": row["repair_id"], "source_sha256": current_hash})
            elif (row["status"] == "adopting" and current_hash == row["candidate_sha256"]
                    and candidate_valid and backup_valid):
                conn.execute("UPDATE recovered_media_repairs SET status='adopted',phase='adopted',decision='adopted',adopted_at=?,error=NULL WHERE repair_id=?", (_now(), row["repair_id"]))
                finalize_item(conn, item, row, restored=False)
                _event(conn, row["case_id"], "reference_repair_adopt_reconciled",
                       actor=row["action_actor"] or row["created_by"], item_id=row["damaged_item_id"],
                       details={"repair_id": row["repair_id"], "source_sha256": current_hash,
                                "backup_sha256": row["backup_sha256"]})
            elif current_hash == row["source_sha256"] and candidate_valid:
                # Adoption never reached the source. A verified backup may
                # remain, but the candidate is still reviewable and retryable.
                conn.execute("UPDATE recovered_media_repairs SET status='completed',phase='review',decision='pending',error='Interrupted before source publication; original remains active' WHERE repair_id=?", (row["repair_id"],))
            elif current_hash == row["candidate_sha256"] and backup_valid:
                stable = "completed" if current_hash == row["source_sha256"] else "adopted"
                decision = "pending" if stable == "completed" else "adopted"
                conn.execute("UPDATE recovered_media_repairs SET status=?,phase=?,decision=?,error='Interrupted action reconciled from source checksum' WHERE repair_id=?", (stable,stable,decision,row["repair_id"]))
                finalize_item(conn, item, row, restored=False)
            else:
                conn.execute("UPDATE recovered_media_repairs SET status='recovery_blocked',phase='manual_review',error='Source, candidate, and backup could not be reconciled safely' WHERE repair_id=?", (row["repair_id"],))
                _event(conn, row["case_id"], "reference_repair_recovery_blocked",
                       actor=row["action_actor"] or row["created_by"], item_id=row["damaged_item_id"],
                       details={"repair_id": row["repair_id"], "source_sha256": current_hash})
        conn.commit()


def reset_runtime_for_tests() -> None:
    with _lock:
        for runtime in _runtimes.values():
            runtime.cancel.set()
            if runtime.process and runtime.process.poll() is None:
                try: os.killpg(runtime.process.pid, signal.SIGKILL)
                except OSError: pass
        threads = list(_threads.values())
    for thread in threads:
        thread.join(timeout=2)
    with _lock:
        _runtimes.clear(); _threads.clear()
