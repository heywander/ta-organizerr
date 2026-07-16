"""Dashboard status, log streaming, and on-demand scan triggers."""
from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import read_logs
from ..library_lock import LIBRARY_MUTATION_LOCK
from ..recovered_channels import organized_recovered_video_ids
from ..symlinks import cleanup_old_folders, process_videos, scan_is_running


bp = Blueprint("status", __name__, url_prefix="/api")


@bp.route("/status")
@requires_auth
def api_status():
    with get_db() as conn:
        videos = []
        for row in conn.execute("SELECT * FROM videos ORDER BY channel, published DESC"):
            videos.append({
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "published": row["published"],
                "symlink": row["symlink"],
                "status": row["status"],
            })
    total = len(videos)
    healthy_recovered_ids = organized_recovered_video_ids()
    linked = sum(
        1
        for video in videos
        if video["status"] in {"linked", "adopted"}
        or (
            video["status"] == "recovered"
            and video["video_id"] in healthy_recovered_ids
        )
    )
    missing = total - linked
    return jsonify({
        "total_videos": total,
        "verified_links": linked,
        "missing_count": missing,
        "videos": videos,
    })


@bp.route("/logs")
@requires_auth
def api_logs():
    try:
        start = max(0, int(request.args.get("start", 0)))
    except (TypeError, ValueError):
        start = 0
    logs, next_index = read_logs(start)
    return jsonify({"logs": logs, "next_index": next_index})


@bp.route("/scan", methods=["POST"])
@requires_auth
def api_scan():
    if scan_is_running():
        return jsonify({"status": "running", "message": "Scan already in progress"}), 202
    threading.Thread(target=process_videos, daemon=True).start()
    return jsonify({"status": "started"})


@bp.route("/cleanup", methods=["POST"])
@requires_auth
def api_cleanup():
    def run_cleanup():
        with LIBRARY_MUTATION_LOCK:
            cleanup_old_folders()

    threading.Thread(target=run_cleanup, daemon=True).start()
    return jsonify({"status": "started"})
