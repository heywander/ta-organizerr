"""Transcode trigger + log endpoints."""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import transcode_log_buffer, transcode_log_lock, tlog
from ..recovery import RECOVERY_ALLOWED_BASES
from ..transcode import detect_encoder, transcode_video
from ..utils import clamp_pagination, path_in_allowed_tree


bp = Blueprint("transcode", __name__, url_prefix="/api/transcode")


@bp.route("/videos")
@requires_auth
def api_transcode_videos():
    page, per_page = clamp_pagination(request.args.get("page"), request.args.get("per_page"))
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS count FROM videos WHERE status = 'missing'"
        ).fetchone()["count"]
        videos = []
        for row in conn.execute(
            "SELECT * FROM videos WHERE status = 'missing' LIMIT ? OFFSET ?",
            (per_page, offset),
        ):
            videos.append({
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "published": row["published"],
                "symlink": row["symlink"],
            })
    return jsonify({
        "videos": videos,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
    })


@bp.route("/start", methods=["POST"])
@requires_auth
def api_transcode_start():
    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if not path_in_allowed_tree(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400

    encoder = detect_encoder()
    tlog(f"🖥️  Selected encoder: {encoder}")
    threading.Thread(target=transcode_video, args=(filepath, encoder), daemon=True).start()
    return jsonify({"message": "Transcode started", "encoder": encoder})


@bp.route("/logs")
@requires_auth
def api_transcode_logs():
    try:
        start = max(0, int(request.args.get("start", 0)))
    except (TypeError, ValueError):
        start = 0
    with transcode_log_lock:
        return jsonify({
            "logs": transcode_log_buffer[start:],
            "next_index": len(transcode_log_buffer),
        })
