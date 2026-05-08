"""Lost-media recovery + bulk delete endpoints with path confinement."""
from __future__ import annotations

import os
import shutil
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
    force_import,
    recover_video_metadata,
    scan_for_unindexed_videos,
)
from ..ta_api import fetch_all_metadata
from ..utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    path_within,
    translate_host_path,
    translate_ta_path,
)


bp = Blueprint("recovery", __name__, url_prefix="/api/recovery")


SCAN_CACHE: dict[str, object] = {
    "status": "idle",
    "results": None,
    "last_run": None,
}
_SCAN_LOCK = threading.Lock()


@bp.route("/scan", methods=["POST"])
@requires_auth
def api_recovery_scan():
    with _SCAN_LOCK:
        if SCAN_CACHE["status"] == "scanning":
            return jsonify({"status": "running", "message": "Scan already in progress"}), 202
        SCAN_CACHE["status"] = "scanning"
        SCAN_CACHE["results"] = None

    def run_scan_async():
        try:
            results = scan_for_unindexed_videos()
            SCAN_CACHE["results"] = results
            SCAN_CACHE["status"] = "done"
            SCAN_CACHE["last_run"] = datetime.now().isoformat()
        except Exception as exc:  # noqa: BLE001 — surface async failures via cache
            SCAN_CACHE["status"] = "error"
            SCAN_CACHE["results"] = str(exc)
            log(f"❌ Async scan failed: {exc}")

    threading.Thread(target=run_scan_async, daemon=True).start()
    return jsonify({"status": "started", "message": "Background scan started"}), 202


@bp.route("/poll", methods=["GET"])
@requires_auth
def api_recovery_poll():
    return jsonify(SCAN_CACHE)


@bp.route("/start", methods=["POST"])
@requires_auth
def api_recovery_start():
    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if not path_within(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400
    success, msg = recover_video_metadata(filepath)
    log(f"Recovery Result for {filepath}: {msg}")
    return jsonify({
        "message": msg,
        "success": success,
        "status": "completed" if success else "failed",
    })


@bp.route("/force", methods=["POST"])
@requires_auth
def api_recovery_force():
    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if not path_within(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400
    log(f"💪 Force Importing (Lost Media): {Path(filepath).name}")
    success, msg = force_import(filepath)
    if success:
        return jsonify({"success": True, "message": msg})
    # 404 if the file is missing, 400/500 otherwise.
    return jsonify({"error": msg}), 500


@bp.route("/delete", methods=["POST"])
@requires_auth
def api_recovery_delete():
    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if not path_within(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400

    p = Path(filepath)
    if not p.exists() or not p.is_file():
        return jsonify({"error": "File not found"}), 404

    vid_id = extract_id_from_filename(p.name)
    try:
        p.unlink()
        if vid_id and is_valid_youtube_id(vid_id):
            with get_db() as conn:
                conn.execute("DELETE FROM lost_media WHERE video_id = ?", (vid_id,))
                conn.execute("DELETE FROM videos WHERE symlink = ?", (filepath,))
                conn.commit()
        log(f"🗑️ Deleted file: {filepath}")
        return jsonify({"success": True, "message": "File deleted"})
    except OSError as exc:
        log(f"❌ Delete failed: {exc}")
        return jsonify({"error": "Delete failed"}), 500


def _resolve_destruct_source(p: Path, vid_id: str | None, video_map: dict) -> Path | None:
    """Find the source file matching ``p``, restricted to allowed bases."""
    candidates: list[Path | None] = []

    if os.path.islink(p):
        try:
            target_raw = os.readlink(p)
            log(f"   [DESTRUCT] Symlink target: {target_raw}")
            candidates.append(translate_host_path(target_raw))
        except OSError as exc:
            log(f"   [DESTRUCT] Warning: Could not read symlink {p}: {exc}")

    if vid_id and is_valid_youtube_id(vid_id):
        meta = video_map.get(vid_id) or {}
        raw_ta_path = meta.get("filesystem_path")
        if raw_ta_path:
            candidates.append(translate_ta_path(raw_ta_path))
            log(f"   [DESTRUCT] API Fallback Path: {candidates[-1]}")

    for candidate in candidates:
        if candidate and candidate.exists() and path_within(candidate, RECOVERY_ALLOWED_BASES):
            return candidate

    if vid_id and is_valid_youtube_id(vid_id):
        log(f"   [DESTRUCT] Final fallback: recursive search for ID {vid_id}")
        video_extensions = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
        for candidate in SOURCE_DIR.rglob(f"*{vid_id}*"):
            if "tcconf" in candidate.parts:
                continue
            if candidate.suffix.lower() not in video_extensions:
                continue
            if candidate.is_file() and path_within(candidate, RECOVERY_ALLOWED_BASES):
                log(f"   [DESTRUCT] Search found match: {candidate}")
                return candidate
    return None


@bp.route("/delete-batch", methods=["POST"])
@requires_auth
def api_recovery_delete_batch():
    data = request.get_json(silent=True) or {}
    paths = data.get("filepaths", [])
    destruct = bool(data.get("destruct_mode", False))

    if not isinstance(paths, list):
        return jsonify({"error": "filepaths must be a list"}), 400

    success_count = 0
    fail_count = 0
    errors: list[str] = []

    log(f"🔥 Batch Delete started. Items: {len(paths)}, Destruct: {destruct}")

    video_map = fetch_all_metadata() if destruct else {}

    for path in paths:
        try:
            p = Path(path)
            if not path_within(p, RECOVERY_ALLOWED_BASES):
                fail_count += 1
                err = f"Path outside allowed directories: {path}"
                log(f"⛔ {err}")
                if err not in errors:
                    errors.append(err)
                continue

            if destruct:
                vid_id = extract_id_from_filename(p.name)
                if not vid_id:
                    with get_db() as conn:
                        row = conn.execute(
                            "SELECT video_id FROM videos WHERE symlink = ?", (path,)
                        ).fetchone()
                        if row:
                            vid_id = row["video_id"]
                source_path = _resolve_destruct_source(p, vid_id, video_map)
                if source_path and source_path.exists():
                    try:
                        source_path.unlink()
                        log(f"☢️ [DESTRUCT] Deleted source: {source_path}")
                    except OSError as exc:
                        log(f"❌ [DESTRUCT] Failed to delete source {source_path}: {exc}")
                        raise
                else:
                    log(f"⚠️ [DESTRUCT] Source file not found for: {path}")

            if os.path.lexists(p):
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                log(f"🗑️ Deleted target: {path}")

                with get_db() as conn:
                    conn.execute("DELETE FROM videos WHERE symlink = ?", (path,))
                    conn.commit()
                log(f"💾 [SYNC] Removed DB record for: {path}")

                parent = p.parent
                if parent != Path(TARGET_DIR) and parent != Path(HIDDEN_DIR):
                    if parent.exists() and not any(parent.iterdir()):
                        try:
                            parent.rmdir()
                            log(f"🧹 [CLEANUP] Removed empty folder: {parent}")
                        except OSError:
                            pass
            else:
                log(f"❓ Target path does not exist (skipping): {path}")

            success_count += 1
        except OSError as exc:
            err_msg = str(exc)
            log(f"❌ Failed to delete {path}: {err_msg}")
            fail_count += 1
            if err_msg not in errors:
                errors.append(err_msg)

    return jsonify({
        "success_count": success_count,
        "fail_count": fail_count,
        "errors": errors[:5],
    })
