"""Static page serving with extension whitelist + SPA-style page handlers."""
from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, send_from_directory

from ..auth import admin_exists, requires_auth


bp = Blueprint("pages", __name__)


_ALLOWED_STATIC_EXTENSIONS = {
    ".js", ".css", ".map", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".txt", ".xml", ".webmanifest",
}


def _send(static_relative: str):
    return send_from_directory(
        current_app.config["STATIC_BUILD_FOLDER"],
        static_relative,
    )


@bp.route("/")
def index():
    # Auth-gate manually so we can also short-circuit to /setup when no admin exists.
    from flask import session
    if not admin_exists():
        return redirect("/setup")
    if not session.get("logged_in"):
        return redirect("/login")
    return _send("index.html")


@bp.route("/login")
def login_page():
    if not admin_exists():
        return redirect("/setup")
    return _send("login/index.html")


@bp.route("/setup")
def setup_page():
    if admin_exists():
        return redirect("/login")
    return _send("setup/index.html")


@bp.route("/maintenance")
@requires_auth
def maintenance_page():
    return _send("maintenance/index.html")


@bp.route("/transcode")
@requires_auth
def transcode_page():
    return _send("transcode/index.html")


@bp.route("/reconciliation")
@requires_auth
def reconciliation_page():
    return _send("reconciliation/index.html")


@bp.route("/salvage")
@requires_auth
def salvage_page():
    return _send("salvage/index.html")


@bp.route("/availability")
@requires_auth
def availability_page():
    return _send("availability/index.html")


@bp.route("/<path:path>")
def static_assets(path):
    """Catch-all for built static files. Whitelist by extension."""
    suffix = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    if suffix not in _ALLOWED_STATIC_EXTENSIONS:
        abort(404)
    return _send(path)
