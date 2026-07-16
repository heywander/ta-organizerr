"""Authentication endpoints: login, logout, status, first-run setup."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from ..auth import (
    AdminAlreadyExists,
    admin_exists,
    check_login,
    create_admin,
    validate_credentials,
)
from ..extensions import limiter
from ..logging_util import log


bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.route("/setup-status", methods=["GET"])
def setup_status():
    return jsonify({"setup_required": not admin_exists()})


@bp.route("/setup", methods=["POST"])
@limiter.limit("5 per minute")
def setup():
    if admin_exists():
        return jsonify({"error": "Setup already complete"}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required"}), 400
    raw_username = data.get("username")
    raw_password = data.get("password")
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    password = raw_password if isinstance(raw_password, str) else ""
    error = validate_credentials(username, password)
    if error:
        return jsonify({"error": error}), 400
    try:
        create_admin(username, password)
    except AdminAlreadyExists:
        return jsonify({"error": "Setup already complete"}), 409
    session.permanent = True
    session["logged_in"] = True
    session["username"] = username
    return jsonify({"success": True})


@bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute; 30 per hour")
def login():
    if not admin_exists():
        return jsonify({"error": "Setup required", "setup_required": True}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required"}), 400
    raw_username = data.get("username")
    raw_password = data.get("password")
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    password = raw_password if isinstance(raw_password, str) else ""
    if check_login(username, password):
        session.permanent = True
        session["logged_in"] = True
        session["username"] = username
        return jsonify({"success": True})
    log(f"⛔ Failed login attempt for user: {username!r} from {request.remote_addr}")
    return jsonify({"error": "Invalid credentials"}), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


@bp.route("/status", methods=["GET"])
def status():
    return jsonify({
        "logged_in": session.get("logged_in", False),
        "username": session.get("username"),
        "setup_required": not admin_exists(),
    })
