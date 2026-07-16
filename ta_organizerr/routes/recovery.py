"""Lost-media recovery + bulk delete endpoints with path confinement."""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from flask import Blueprint, current_app, jsonify, request

from ..auth import requires_auth
from ..config import HIDDEN_DIR, SOURCE_DIR, TARGET_DIR
from ..db import get_db
from ..logging_util import log
from ..library_lock import LIBRARY_MUTATION_LOCK
from ..recovery import (
    RECOVERY_ALLOWED_BASES,
    _delete_candidate_error,
    _parse_delete_batch_payload,
    _resolve_destruct_source,
    force_import,
    make_source_lookup,
    recover_video_metadata,
    scan_for_unindexed_videos,
)
from ..ta_api import MetadataFetchError, fetch_all_metadata
from ..transcode import delete_source_if_idle
from ..utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    path_in_allowed_tree,
    path_within,
    resolve_media_source,
)


bp = Blueprint("recovery", __name__, url_prefix="/api/recovery")
_MAX_PATH_LENGTH = 4096
_STREAM_CHUNK_SIZE = 128 * 1024
_AUDIO_PROBE_TIMEOUT_SECONDS = 15
_SINGLE_BYTE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)
_RAW_PREVIEW_GUARD = threading.BoundedSemaphore(value=2)
_AUDIO_PREVIEW_GUARD = threading.BoundedSemaphore(value=1)

# These are the media containers the recovery UI may preview.  Keeping an
# explicit allow-list prevents this authenticated file-serving endpoint from
# becoming a way to read arbitrary files that happen to live under a media
# mount.  Codec support still depends on the browser.
_PREVIEW_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".avi": "video/x-msvideo",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".ogv": "video/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "video/webm",
}

# FFmpeg must never auto-detect a playlist or other indirection-capable
# demuxer from attacker-controlled archive bytes.  Each accepted suffix is
# pinned to the corresponding ordinary media demuxer, and the subprocess
# receives the already-open file only through stdin with the ``pipe`` protocol.
_PREVIEW_MEDIA_DEMUXERS = {
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


SCAN_CACHE: dict[str, object] = {
    "status": "idle",
    "results": None,
    "last_run": None,
}
_SCAN_LOCK = threading.Lock()


class _CleanupOnce:
    """Make streaming cleanup safe across EOF, disconnect, and response close."""

    def __init__(self, callback: Callable[[], None]):
        self._callback = callback
        self._lock = threading.Lock()
        self._completed = False

    def __call__(self) -> None:
        with self._lock:
            if self._completed:
                return
            self._completed = True
        self._callback()


def _validated_entry_path(filepath: str | None) -> tuple[Path | None, tuple[object, int] | None]:
    if not isinstance(filepath, str) or not filepath or "\x00" in filepath:
        return None, (jsonify({"error": "No filepath provided"}), 400)
    if len(filepath) > _MAX_PATH_LENGTH:
        return None, (jsonify({"error": "Filepath is too long"}), 400)
    path = Path(filepath)
    if not path_in_allowed_tree(path, RECOVERY_ALLOWED_BASES):
        return None, (jsonify({"error": "Path outside allowed directories"}), 400)
    return path, None


def _validated_media_source(filepath: str | None) -> tuple[Path | None, tuple[object, int] | None]:
    path, error = _validated_entry_path(filepath)
    if error:
        return None, error
    source = resolve_media_source(path)
    if not source or not path_within(source, RECOVERY_ALLOWED_BASES):
        return None, (jsonify({"error": "Resolved media path outside allowed directories"}), 400)
    return source, None


def _preview_headers(response):
    """Apply privacy and type-sniffing headers to preview responses."""
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@bp.after_request
def _protect_preview_responses(response):
    """Cover auth failures too, which return before either preview view runs."""
    if request.endpoint in {
        "recovery.api_recovery_preview",
        "recovery.api_recovery_preview_audio",
    }:
        return _preview_headers(response)
    return response


def _preview_error(
    message: str,
    status: int,
    *,
    size: int | None = None,
    retry_after: int | None = None,
):
    response = jsonify({"error": message})
    response.status_code = status
    if status == 416 and size is not None:
        response.headers["Content-Range"] = f"bytes */{size}"
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return _preview_headers(response)


def _open_preview_file(
    source: Path,
) -> tuple[BinaryIO | None, int | None, str | None, str | None]:
    """Open and validate a regular media file without trusting a raced path.

    The route validates the requested entry and its resolved target first.  It
    then opens the canonical target with ``O_NOFOLLOW`` and verifies the actual
    opened descriptor through ``/proc/self/fd``.  This closes the usual gap in
    which a symlinked ancestor could be swapped between validation and open.
    """
    source_mimetype = _PREVIEW_MEDIA_TYPES.get(source.suffix.lower())
    if source_mimetype is None:
        return None, None, None, None

    try:
        canonical_source = source.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise FileNotFoundError from None
    if not path_within(canonical_source, RECOVERY_ALLOWED_BASES):
        raise PermissionError("Resolved media path outside allowed directories")
    # Validate the final target's own type as well. A media-looking symlink may
    # not be used to disguise a non-media file inside an allowed mount.
    canonical_suffix = canonical_source.suffix.lower()
    mimetype = _PREVIEW_MEDIA_TYPES.get(canonical_suffix)
    demuxer = _PREVIEW_MEDIA_DEMUXERS.get(canonical_suffix)
    if mimetype is None or demuxer is None:
        return None, None, None, None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # A malicious FIFO must not be able to block a worker before fstat rejects
    # it. O_NONBLOCK has no effect on ordinary media files.
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(canonical_source, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("Preview source is not a regular file")

        proc_fd = Path(f"/proc/self/fd/{fd}")
        try:
            opened_path = proc_fd.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            # The supported deployment is Linux. Fail closed if /proc is not
            # mounted or the file was unlinked during validation.
            raise PermissionError("Could not validate opened media file") from None
        if not path_within(opened_path, RECOVERY_ALLOWED_BASES):
            raise PermissionError("Opened media path outside allowed directories")

        return os.fdopen(fd, "rb"), file_stat.st_size, mimetype, demuxer
    except Exception:
        os.close(fd)
        raise


def _requested_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive single byte range, or raise for invalid ranges."""
    if value is None:
        return None
    match = _SINGLE_BYTE_RANGE_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Invalid or multiple byte range")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("Empty byte range")
    if size <= 0:
        raise ValueError("Empty files do not satisfy byte ranges")

    if first:
        start = int(first)
        if start >= size:
            raise ValueError("Byte range starts past end of file")
        end = min(int(last), size - 1) if last else size - 1
        if end < start:
            raise ValueError("Byte range ends before it starts")
        return start, end

    suffix_length = int(last)
    if suffix_length <= 0:
        raise ValueError("Suffix byte range must be positive")
    length = min(suffix_length, size)
    return size - length, size - 1


def _iter_file_range(
    file_handle: BinaryIO,
    start: int,
    length: int,
    cleanup: Callable[[], None],
) -> Iterator[bytes]:
    """Stream exactly one bounded region and always close its file descriptor."""
    try:
        file_handle.seek(start)
        remaining = length
        while remaining:
            chunk = file_handle.read(min(remaining, _STREAM_CHUNK_SIZE))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        cleanup()


def _audio_probe_command(demuxer: str) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "pipe",
        "-format_whitelist",
        demuxer,
        "-f",
        demuxer,
        "-probesize",
        "10000000",
        "-analyzeduration",
        "10000000",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        "pipe:0",
    ]


def _audio_transcode_command(demuxer: str) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "pipe",
        "-format_whitelist",
        demuxer,
        "-f",
        demuxer,
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-threads",
        "1",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-f",
        "mp3",
        "pipe:1",
    ]


def _stop_and_reap_process(process: subprocess.Popen[bytes]) -> None:
    """Close pipes, stop a live ffmpeg, and always attempt to reap it."""
    stdout = process.stdout
    if stdout is not None:
        try:
            stdout.close()
        except OSError:
            pass
    try:
        running = process.poll() is None
    except OSError:
        running = True
    if running:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _iter_audio_transcode(
    process: subprocess.Popen[bytes],
    cleanup: Callable[[], None],
) -> Iterator[bytes]:
    """Yield ffmpeg output and clean up promptly at EOF or client disconnect."""
    try:
        if process.stdout is None:
            return
        while True:
            chunk = process.stdout.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
        try:
            returncode = process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            returncode = None
        if returncode not in (None, 0):
            log(f"⚠️ Audio preview transcoder exited with status {returncode}")
    finally:
        cleanup()


def _cleanup_empty_organized_parent(parent: Path) -> None:
    """Best-effort cleanup limited to descendants of organized output roots."""
    try:
        resolved_parent = parent.resolve(strict=False)
        resolved_roots = tuple(
            root.resolve(strict=False) for root in (Path(TARGET_DIR), Path(HIDDEN_DIR))
        )
        if not any(
            resolved_parent != root and resolved_parent.is_relative_to(root)
            for root in resolved_roots
        ):
            return
        if parent.is_symlink() or not parent.exists() or any(parent.iterdir()):
            return
        parent.rmdir()
        log(f"🧹 [CLEANUP] Removed empty folder: {parent}")
    except (OSError, RuntimeError) as exc:
        log(f"⚠️ Could not clean empty organized folder {parent}: {exc}")


@bp.route("/scan", methods=["POST"])
@requires_auth
def api_recovery_scan():
    with _SCAN_LOCK:
        if SCAN_CACHE["status"] == "scanning":
            return jsonify({"status": "running", "message": "Scan already in progress"}), 202
        SCAN_CACHE["status"] = "scanning"
        SCAN_CACHE["results"] = None
        SCAN_CACHE["last_run"] = None

    def run_scan_async():
        try:
            results = scan_for_unindexed_videos()
            with _SCAN_LOCK:
                SCAN_CACHE["results"] = results
                SCAN_CACHE["status"] = "done"
                SCAN_CACHE["last_run"] = datetime.now().isoformat()
        except Exception as exc:  # noqa: BLE001 — surface async failures via cache
            with _SCAN_LOCK:
                SCAN_CACHE["status"] = "error"
                SCAN_CACHE["results"] = str(exc)
            log(f"❌ Async scan failed: {exc}")

    threading.Thread(target=run_scan_async, daemon=True).start()
    return jsonify({"status": "started", "message": "Background scan started"}), 202


@bp.route("/poll", methods=["GET"])
@requires_auth
def api_recovery_poll():
    with _SCAN_LOCK:
        snapshot = dict(SCAN_CACHE)
    return jsonify(snapshot)


@bp.route("/preview", methods=["GET", "HEAD"])
@requires_auth
def api_recovery_preview():
    """Stream one confined recovery media file to the authenticated browser."""
    source, error = _validated_media_source(request.args.get("filepath"))
    if error:
        response, status = error
        response.status_code = status
        return _preview_headers(response)

    try:
        file_handle, size, mimetype, _demuxer = _open_preview_file(source)
    except FileNotFoundError:
        return _preview_error("Media file not found", 404)
    except PermissionError:
        return _preview_error("Media file is not accessible", 403)
    except OSError:
        return _preview_error("Path is not a regular media file", 400)

    if file_handle is None or size is None or mimetype is None:
        return _preview_error("Unsupported media type", 415)

    raw_guard = _RAW_PREVIEW_GUARD
    if not raw_guard.acquire(blocking=False):
        file_handle.close()
        return _preview_error(
            "Too many media previews are already active",
            429,
            retry_after=2,
        )

    def release_raw_preview() -> None:
        try:
            file_handle.close()
        finally:
            raw_guard.release()

    cleanup = _CleanupOnce(release_raw_preview)

    try:
        byte_range = _requested_byte_range(request.headers.get("Range"), size)
    except (TypeError, ValueError):
        cleanup()
        return _preview_error("Requested byte range is not satisfiable", 416, size=size)

    if byte_range is None:
        start, end = 0, max(0, size - 1)
        status = 200
        content_length = size
    else:
        start, end = byte_range
        status = 206
        content_length = end - start + 1

    if request.method == "HEAD":
        cleanup()
        response = current_app.response_class(status=status, content_type=mimetype)
    else:
        try:
            response = current_app.response_class(
                _iter_file_range(file_handle, start, content_length, cleanup),
                status=status,
                content_type=mimetype,
                direct_passthrough=True,
            )
        except Exception:
            cleanup()
            raise
        # Also covers a server closing the response before the iterator starts.
        response.call_on_close(cleanup)

    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Disposition"] = "inline"
    response.headers["Content-Length"] = str(content_length)
    if status == 206:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return _preview_headers(response)


@bp.route("/preview-audio", methods=["GET", "HEAD"])
@requires_auth
def api_recovery_preview_audio():
    """Transcode one confined media file to a low-cost MP3 audio stream."""
    source, error = _validated_media_source(request.args.get("filepath"))
    if error:
        response, status = error
        response.status_code = status
        return _preview_headers(response)

    try:
        file_handle, _size, _mimetype, demuxer = _open_preview_file(source)
    except FileNotFoundError:
        return _preview_error("Media file not found", 404)
    except PermissionError:
        return _preview_error("Media file is not accessible", 403)
    except OSError:
        return _preview_error("Path is not a regular media file", 400)

    if file_handle is None or demuxer is None:
        return _preview_error("Unsupported media type", 415)

    audio_guard = _AUDIO_PREVIEW_GUARD
    if not audio_guard.acquire(blocking=False):
        file_handle.close()
        return _preview_error(
            "Another compatibility audio preview is already active",
            429,
            retry_after=2,
        )

    def release_source_and_slot() -> None:
        try:
            file_handle.close()
        finally:
            audio_guard.release()

    early_cleanup = _CleanupOnce(release_source_and_slot)
    try:
        probe = subprocess.run(
            _audio_probe_command(demuxer),
            stdin=file_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_AUDIO_PROBE_TIMEOUT_SECONDS,
            check=False,
            close_fds=True,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        early_cleanup()
        return _preview_error(
            "Timed out while inspecting the media audio stream",
            503,
            retry_after=5,
        )
    except OSError:
        early_cleanup()
        return _preview_error(
            "Audio compatibility service is unavailable",
            503,
            retry_after=5,
        )

    if probe.returncode != 0:
        early_cleanup()
        return _preview_error("Media audio stream could not be inspected", 422)
    probe_output = probe.stdout or ""
    if isinstance(probe_output, bytes):
        probe_output = probe_output.decode("utf-8", errors="replace")
    if not probe_output.strip():
        early_cleanup()
        return _preview_error("Media file has no audio stream", 422)

    if request.method == "HEAD":
        early_cleanup()
        response = current_app.response_class(status=200, content_type="audio/mpeg")
        response.headers["Content-Disposition"] = "inline"
        return _preview_headers(response)

    try:
        file_handle.seek(0)
        process = subprocess.Popen(
            _audio_transcode_command(demuxer),
            stdin=file_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
            bufsize=0,
        )
    except OSError:
        early_cleanup()
        return _preview_error(
            "Audio compatibility service is unavailable",
            503,
            retry_after=5,
        )

    if process.stdout is None:
        _stop_and_reap_process(process)
        early_cleanup()
        return _preview_error(
            "Audio compatibility service failed to start",
            503,
            retry_after=5,
        )

    # Popen duplicated the validated descriptor onto stdin. The parent must not
    # retain an additional source-file descriptor for the duration of playback.
    try:
        file_handle.close()
    except OSError:
        _stop_and_reap_process(process)
        early_cleanup()
        return _preview_error(
            "Audio compatibility service failed to start",
            503,
            retry_after=5,
        )

    def release_audio_preview() -> None:
        try:
            _stop_and_reap_process(process)
        finally:
            audio_guard.release()

    cleanup = _CleanupOnce(release_audio_preview)
    try:
        returncode = process.poll()
    except OSError:
        cleanup()
        return _preview_error("Audio compatibility service failed to start", 502)
    if returncode not in (None, 0):
        cleanup()
        return _preview_error("Audio compatibility service failed to start", 502)

    try:
        response = current_app.response_class(
            _iter_audio_transcode(process, cleanup),
            status=200,
            content_type="audio/mpeg",
            direct_passthrough=True,
        )
    except Exception:
        cleanup()
        raise
    response.call_on_close(cleanup)

    response.headers["Content-Disposition"] = "inline"
    return _preview_headers(response)


@bp.route("/start", methods=["POST"])
@requires_auth
def api_recovery_start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    media_path, error = _validated_media_source(data.get("filepath"))
    if error:
        return error
    success, msg = recover_video_metadata(str(media_path))
    log(f"Recovery Result for {media_path}: {msg}")
    return jsonify({
        "message": msg,
        "success": success,
        "status": "completed" if success else "failed",
    })


@bp.route("/force", methods=["POST"])
@requires_auth
def api_recovery_force():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    media_path, error = _validated_media_source(data.get("filepath"))
    if error:
        return error
    log(f"💪 Force Importing (Lost Media): {media_path.name}")
    success, msg = force_import(str(media_path))
    if success:
        return jsonify({"success": True, "message": msg})
    # 404 if the file is missing, 400/500 otherwise.
    return jsonify({"error": msg}), 500


@bp.route("/delete", methods=["POST"])
@requires_auth
def api_recovery_delete():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    p, error = _validated_entry_path(data.get("filepath"))
    if error:
        return error

    with LIBRARY_MUTATION_LOCK:
        # Revalidate only after serialization so a reconciliation action cannot
        # observe or act on a half-completed generic delete.
        candidate_error = _delete_candidate_error(
            p,
            destruct=False,
            allowed_bases=RECOVERY_ALLOWED_BASES,
            source_dir=SOURCE_DIR,
        )
        if candidate_error:
            status = 404 if candidate_error == "File not found" else 400
            return jsonify({"error": candidate_error}), status

        vid_id = extract_id_from_filename(p.name)
        try:
            p.unlink()
            with get_db() as conn:
                if vid_id and is_valid_youtube_id(vid_id):
                    conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
                conn.execute("DELETE FROM videos WHERE symlink = ?", (str(p),))
                conn.commit()
            log(f"🗑️ Deleted file: {p}")
            return jsonify({"success": True, "message": "File deleted"})
        except OSError as exc:
            log(f"❌ Delete failed: {exc}")
            return jsonify({"error": "Delete failed"}), 500
        except sqlite3.Error as exc:
            log(
                "❌ File deletion completed but database synchronization failed "
                f"for {p}: {exc}. Run a scan to reconcile state."
            )
            return jsonify({
                "error": "File deleted, but database sync failed; run a scan to reconcile",
                "filesystem_deleted": True,
                "db_sync_failed": True,
            }), 500


@bp.route("/delete-batch", methods=["POST"])
@requires_auth
def api_recovery_delete_batch():
    paths, destruct, payload_error = _parse_delete_batch_payload(
        request.get_json(silent=True)
    )
    if payload_error or paths is None:
        return jsonify({"error": payload_error or "Invalid delete request"}), 400

    success_count = 0
    fail_count = 0
    errors: list[str] = []
    valid_paths: list[tuple[str, Path]] = []

    log(f"🔥 Batch Delete started. Items: {len(paths)}, Destruct: {destruct}")

    for path in paths:
        p = Path(path)
        candidate_error = _delete_candidate_error(
            p,
            destruct=destruct,
            allowed_bases=RECOVERY_ALLOWED_BASES,
            source_dir=SOURCE_DIR,
        )
        if candidate_error:
            fail_count += 1
            err = f"{candidate_error}: {path}"
            log(f"⛔ {err}")
            if err not in errors:
                errors.append(err)
            continue
        valid_paths.append((path, p))

    try:
        video_map = fetch_all_metadata() if destruct and valid_paths else {}
    except MetadataFetchError as exc:
        log(f"❌ Batch delete aborted before changes; metadata fetch failed: {exc}")
        return jsonify({"error": "Could not fetch TubeArchivist metadata"}), 502

    source_lookup = make_source_lookup(SOURCE_DIR) if destruct else None

    with LIBRARY_MUTATION_LOCK:
        for path, p in valid_paths:
            filesystem_changed = False
            try:
                # The first pass filters obvious errors; this second pass is
                # authoritative because it runs under the shared library lock.
                candidate_error = _delete_candidate_error(
                    p,
                    destruct=destruct,
                    allowed_bases=RECOVERY_ALLOWED_BASES,
                    source_dir=SOURCE_DIR,
                )
                if candidate_error:
                    raise OSError(candidate_error)

                vid_id = extract_id_from_filename(p.name)
                if destruct:
                    if not vid_id or not is_valid_youtube_id(vid_id):
                        with get_db() as conn:
                            row = conn.execute(
                                "SELECT video_id FROM videos WHERE symlink = ?", (path,)
                            ).fetchone()
                            if row:
                                vid_id = row["video_id"]
                    if not vid_id or not is_valid_youtube_id(vid_id):
                        source_hint = resolve_media_source(p)
                        if source_hint:
                            vid_id = extract_id_from_filename(source_hint.name)
                    source_path = _resolve_destruct_source(
                        p,
                        vid_id,
                        video_map,
                        source_dir=SOURCE_DIR,
                        fallback_lookup=source_lookup,
                    )
                    if source_path is None:
                        raise OSError(
                            f"Exact source file not found for video ID {vid_id or 'unknown'}"
                        )
                    delete_source_if_idle(source_path)
                    filesystem_changed = True
                    log(f"☢️ [DESTRUCT] Deleted source: {source_path}")

                if os.path.lexists(p):
                    p.unlink()
                    filesystem_changed = True
                    log(f"🗑️ Deleted target: {path}")

                    _cleanup_empty_organized_parent(p.parent)
                else:
                    # This is expected when destruct mode was given a SOURCE_DIR
                    # entry: source and target are the same directory entry.
                    if not destruct or not path_in_allowed_tree(p, (SOURCE_DIR,)):
                        raise OSError(f"Target path disappeared before deletion: {path}")

                with get_db() as conn:
                    conn.execute("DELETE FROM videos WHERE symlink = ?", (path,))
                    if vid_id and is_valid_youtube_id(vid_id):
                        conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
                    conn.commit()
                log(f"💾 [SYNC] Removed DB record for: {path}")

                success_count += 1
            except OSError as exc:
                err_msg = str(exc)
                if filesystem_changed:
                    err_msg = f"Partial filesystem deletion; reconciliation required: {err_msg}"
                log(f"❌ Failed to delete {path}: {err_msg}")
                fail_count += 1
                if err_msg not in errors:
                    errors.append(err_msg)
            except sqlite3.Error as exc:
                if filesystem_changed:
                    err_msg = (
                        "filesystem_deleted/db_sync_failed; run a scan to reconcile: "
                        f"{path}"
                    )
                else:
                    err_msg = f"Database lookup failed before deletion: {path}"
                log(f"❌ {err_msg}: {exc}")
                fail_count += 1
                if err_msg not in errors:
                    errors.append(err_msg)

    return jsonify({
        "success_count": success_count,
        "fail_count": fail_count,
        "errors": errors[:5],
    })
