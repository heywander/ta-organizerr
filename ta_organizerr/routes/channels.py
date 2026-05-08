"""Channel listing and per-channel video lookup."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..ta_api import fetch_all_metadata


bp = Blueprint("channels", __name__, url_prefix="/api/channels")


@bp.route("", methods=["GET"])
@requires_auth
def api_get_channels():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT channel FROM videos WHERE channel IS NOT NULL ORDER BY channel ASC"
        ).fetchall()
    channels = [row["channel"] for row in rows if row["channel"]]
    return jsonify(channels)


@bp.route("/videos", methods=["GET"])
@requires_auth
def api_get_channel_videos():
    channel_name = (request.args.get("channel") or "").strip()
    if not channel_name:
        return jsonify({"error": "No channel name provided"}), 400

    video_map = fetch_all_metadata()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT video_id, title, symlink, is_live FROM videos WHERE channel = ? ORDER BY published DESC",
            (channel_name,),
        ).fetchall()

    videos = []
    for row in rows:
        vid_id = row["video_id"]
        meta = video_map.get(vid_id, {})
        videos.append({
            "video_id": vid_id,
            "title": row["title"],
            "path": row["symlink"],
            "filename": Path(row["symlink"]).name if row["symlink"] else meta.get("title"),
            "source_path": meta.get("filesystem_path"),
            "ta_source": meta.get("channel_name", channel_name),
            "is_live": bool(row["is_live"]) if "is_live" in row.keys() else False,
        })
    return jsonify(videos)
