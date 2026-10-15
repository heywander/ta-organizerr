"""Transcode trigger + log endpoints."""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import transcode_log_buffer, transcode_log_lock, tlog
from ..recovery import RECOVERY_ALLOWED_BASES
from ..transcode import (
    active_jobs,
    detect_encoder,
    scan_codecs,
    transcode_is_running,
    transcode_video,
)
from ..utils import clamp_pagination, path_in_allowed_tree


bp = Blueprint("transcode", __name__, url_prefix="/api/transcode")


# Linked videos that are not yet H.264/AAC, or whose codecs are still unknown.
_QUEUE_WHERE = (
    "status = 'linked' AND ("
    "video_codec IS NULL OR video_codec != 'h264' OR audio_codec IS NULL OR audio_codec != 'aac')"
)


@bp.route("/videos")
@requires_auth
def api_transcode_videos():
    page, per_page = clamp_pagination(request.args.get("page"), request.args.get("per_page"))
    offset = (page - 1) * per_page
    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM videos WHERE {_QUEUE_WHERE}"
        ).fetchone()["count"]
        unknown = conn.execute(
            "SELECT COUNT(*) AS count FROM videos WHERE status = 'linked' AND video_codec IS NULL"
        ).fetchone()["count"]
        videos = []
        for row in conn.execute(
            f"SELECT * FROM videos WHERE {_QUEUE_WHERE} "
            "ORDER BY video_codec IS NULL, channel, published DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ):
            videos.append({
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "published": row["published"],
                "symlink": row["symlink"],
                "video_codec": row["video_codec"],
                "audio_codec": row["audio_codec"],
            })
    return jsonify({
        "videos": videos,
        "total": total,
        "unknown": unknown,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if per_page else 1,
        "running": transcode_is_running(),
        "active": active_jobs(),
    })


_CODEC_SCAN_LOCK = threading.Lock()


@bp.route("/scan", methods=["POST"])
@requires_auth
def api_transcode_scan():
    """Probe codecs of linked videos in the background so the queue is accurate."""
    if _CODEC_SCAN_LOCK.locked():
        return jsonify({"status": "running", "message": "Codec scan already in progress"}), 202

    def run():
        with _CODEC_SCAN_LOCK:
            scan_codecs()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "message": "Codec scan started"}), 202


@bp.route("/start", methods=["POST"])
@requires_auth
def api_transcode_start():
    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"error": "No filepath provided"}), 400
    if not path_in_allowed_tree(Path(filepath), RECOVERY_ALLOWED_BASES):
        return jsonify({"error": "Path outside allowed directories"}), 400

    if filepath in active_jobs():
        return jsonify({"message": "Already queued", "encoder": None}), 202
    encoder = detect_encoder()
    tlog(f"🖥️  Selected encoder: {encoder}")
    threading.Thread(target=transcode_video, args=(filepath, encoder), daemon=True).start()
    queued = transcode_is_running()
    return jsonify({
        "message": "Transcode queued behind the running job" if queued else "Transcode started",
        "encoder": encoder,
    })


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
