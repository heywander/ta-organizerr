"""Lost-media recovery + bulk delete endpoints with path confinement."""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..config import HIDDEN_DIR, SOURCE_DIR, TARGET_DIR
from ..db import get_db
from ..logging_util import log
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


SCAN_CACHE: dict[str, object] = {
    "status": "idle",
    "results": None,
    "last_run": None,
}
_SCAN_LOCK = threading.Lock()


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

    for path, p in valid_paths:
        filesystem_changed = False
        try:
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
