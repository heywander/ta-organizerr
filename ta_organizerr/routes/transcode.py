"""Transcode trigger + log endpoints."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import read_transcode_logs, tlog
from ..library_lock import LIBRARY_MUTATION_LOCK
from ..recovery import RECOVERY_ALLOWED_BASES
from ..recovered_channels import is_recovered_media_path
from ..transcode import detect_encoder, start_transcode_job
from ..utils import clamp_pagination, path_in_allowed_tree


bp = Blueprint("transcode", __name__, url_prefix="/api/transcode")
_MAX_PATH_LENGTH = 4096


@bp.route("/videos")
@requires_auth
def api_transcode_videos():
    page, per_page = clamp_pagination(request.args.get("page"), request.args.get("per_page"))
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS count FROM videos
            WHERE status = 'linked' AND symlink IS NOT NULL
              AND (
                  lower(symlink) LIKE '%.mp4'
                  OR lower(symlink) LIKE '%.mkv'
                  OR lower(symlink) LIKE '%.mov'
              )
            """
        ).fetchone()["count"]
        videos = []
        for row in conn.execute(
            """
            SELECT * FROM videos
            WHERE status = 'linked' AND symlink IS NOT NULL
              AND (
                  lower(symlink) LIKE '%.mp4'
                  OR lower(symlink) LIKE '%.mkv'
                  OR lower(symlink) LIKE '%.mov'
              )
            ORDER BY channel, published DESC
            LIMIT ? OFFSET ?
            """,
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
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    filepath = data.get("filepath")
    if not isinstance(filepath, str) or not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if "\x00" in filepath or len(filepath) > _MAX_PATH_LENGTH:
        return jsonify({"error": "Invalid filepath"}), 400
    if not path_in_allowed_tree(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400
    with LIBRARY_MUTATION_LOCK:
        if is_recovered_media_path(filepath):
            return jsonify({
                "error": (
                    "Recovered-channel source media is immutable. Transcoding this "
                    "path in place is disabled."
                )
            }), 409

        encoder = detect_encoder()
        tlog(f"🖥️  Selected encoder: {encoder}")
        started, message = start_transcode_job(filepath, encoder)
    if not started:
        message_lower = message.lower()
        if "already" in message_lower or "currently in use" in message_lower:
            status = 409
        elif "busy" in message_lower:
            status = 429
        else:
            status = 400
        return jsonify({"error": message}), status
    return jsonify({"message": message, "encoder": encoder}), 202


@bp.route("/logs")
@requires_auth
def api_transcode_logs():
    try:
        start = max(0, int(request.args.get("start", 0)))
    except (TypeError, ValueError):
        start = 0
    logs, next_index = read_transcode_logs(start)
    return jsonify({"logs": logs, "next_index": next_index})
