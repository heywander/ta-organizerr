"""Hidden-channel CRUD."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..auth import requires_auth
from ..db import get_db
from ..logging_util import log


bp = Blueprint("hidden", __name__, url_prefix="/api/hidden")


@bp.route("", methods=["GET"])
@requires_auth
def api_get_hidden():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT channel_name FROM hidden_channels ORDER BY channel_name"
        ).fetchall()
    return jsonify({"channels": [row["channel_name"] for row in rows]})


@bp.route("", methods=["POST"])
@requires_auth
def api_add_hidden():
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").strip()
    if not channel:
        return jsonify({"error": "No channel name provided"}), 400
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO hidden_channels (channel_name) VALUES (?)", (channel,))
        conn.commit()
    log(f"🙈 Added to hidden list: {channel}")
    return jsonify({"success": True})


@bp.route("", methods=["DELETE"])
@requires_auth
def api_remove_hidden():
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").strip()
    if not channel:
        return jsonify({"error": "No channel name provided"}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM hidden_channels WHERE channel_name = ?", (channel,))
        conn.commit()
    log(f"👁️ Removed from hidden list: {channel}")
    return jsonify({"success": True})
