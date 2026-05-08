"""Dashboard status, log streaming, and on-demand scan triggers."""
from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import log_buffer, log_lock
from ..symlinks import cleanup_old_folders, process_videos


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
    linked = sum(1 for v in videos if v["status"] == "linked")
    missing = sum(1 for v in videos if v["status"] == "missing")
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
    with log_lock:
        return jsonify({
            "logs": log_buffer[start:],
            "next_index": len(log_buffer),
        })


@bp.route("/scan", methods=["POST"])
@requires_auth
def api_scan():
    threading.Thread(target=process_videos, daemon=True).start()
    return jsonify({"status": "started"})


@bp.route("/cleanup", methods=["POST"])
@requires_auth
def api_cleanup():
    threading.Thread(target=cleanup_old_folders, daemon=True).start()
    return jsonify({"status": "started"})
