"""Flask application factory."""
from __future__ import annotations

import ipaddress
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, abort, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import get_or_create_secret_key
from .config import (
    ALLOWED_IPS,
    FLASK_SECRET_KEY_ENV,
    HOST_SOURCE_BASE,
    HOST_SOURCE_BASE_CONFIGURED,
    SESSION_COOKIE_SECURE,
    SESSION_LIFETIME_SECONDS,
    STATIC_FOLDER,
    TRUSTED_PROXY_COUNT,
)
from .db import init_db_with_retry
from .extensions import limiter
from .logging_util import log


def _enforce_ip_allowlist():
    if not ALLOWED_IPS:
        return
    client_ip = request.remote_addr
    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        log(f"⛔ Invalid IP format: {client_ip}")
        abort(403)
    for allowed in ALLOWED_IPS:
        if not allowed:
            continue
        try:
            if "/" in allowed:
                if ip_obj in ipaddress.ip_network(allowed, strict=False):
                    return
            elif ip_obj == ipaddress.ip_address(allowed):
                return
        except ValueError:
            continue
    log(f"⛔ Access denied for IP: {client_ip}")
    abort(403)


_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _enforce_same_origin():
    """Reject cross-site state-changing requests (CSRF defence).

    The session cookie is SameSite=Lax, which already blocks most cross-site
    POSTs, but browsers send ``Origin`` (or ``Sec-Fetch-Site``) on every
    fetch/XHR, so we can verify it without changing any client code. Requests
    with neither header (curl, scripts) are left alone: they carry no cookie.
    """
    if request.method not in _STATE_CHANGING_METHODS:
        return
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc.lower() != request.host.lower():
            log(f"⛔ Cross-origin {request.method} blocked from {origin} ({request.remote_addr})")
            abort(403)
        return
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        log(f"⛔ Cross-site {request.method} blocked (Sec-Fetch-Site={fetch_site}, {request.remote_addr})")
        abort(403)


def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "media-src 'self'; "
        "frame-ancestors 'none'",
    )
    if SESSION_COOKIE_SECURE:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


def create_app(initialize_db: bool = True) -> Flask:
    # Static files are served through routes/pages.py so built HTML shells cannot
    # bypass their authentication gates via Flask's automatic static catch-all.
    app = Flask(__name__, static_folder=None)
    app.config["STATIC_BUILD_FOLDER"] = STATIC_FOLDER
    if TRUSTED_PROXY_COUNT > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=TRUSTED_PROXY_COUNT,
            x_proto=TRUSTED_PROXY_COUNT,
            x_host=TRUSTED_PROXY_COUNT,
        )
    if not HOST_SOURCE_BASE_CONFIGURED:
        log(
            "⚠️ HOST_SOURCE_BASE is unset; using legacy fallback "
            f"{HOST_SOURCE_BASE}. Set it explicitly before the fallback is removed."
        )
    if initialize_db:
        init_db_with_retry()
        # Resolve any crash-left channel-folder transaction before the
        # scheduler can recreate paths from a stale registry component.
        from .availability_designation import recover_pending_designations

        recover_pending_designations()
        # Correct derived availability-v1 states only after any crash-left
        # folder transaction has resolved. A repair revision must never make a
        # pending designation journal appear mixed during recovery.
        from .availability import reclassify_legacy_channel_observations

        reclassify_legacy_channel_observations()

        # Long-running UNTRUNC and source replacement work is journaled in
        # SQLite. Reconcile any crash-left action before serving mutations.
        from .reference_repair import recover_interrupted_repairs

        recover_interrupted_repairs()

    app.secret_key = get_or_create_secret_key(FLASK_SECRET_KEY_ENV)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_LIFETIME_SECONDS),
        JSON_SORT_KEYS=False,
    )

    limiter.init_app(app)
    app.before_request(_enforce_ip_allowlist)
    app.before_request(_enforce_same_origin)
    app.after_request(_apply_security_headers)

    # Register blueprints. Imported lazily to avoid circular imports during
    # startup (pages.py reads admin_exists which touches the DB).
    from .routes.auth import bp as auth_bp
    from .routes.availability import bp as availability_bp
    from .routes.channels import bp as channels_bp
    from .routes.hidden import bp as hidden_bp
    from .routes.maintenance import bp as maintenance_bp
    from .routes.pages import bp as pages_bp
    from .routes.recovery import bp as recovery_bp
    from .routes.recovered_channels import bp as recovered_channels_bp
    from .routes.reconciliation import bp as reconciliation_bp
    from .routes.salvage import bp as salvage_bp
    from .routes.status import bp as status_bp
    from .routes.transcode import bp as transcode_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(availability_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(maintenance_bp)
    app.register_blueprint(transcode_bp)
    app.register_blueprint(recovery_bp)
    app.register_blueprint(recovered_channels_bp)
    app.register_blueprint(reconciliation_bp)
    app.register_blueprint(salvage_bp)
    app.register_blueprint(channels_bp)
    app.register_blueprint(hidden_bp)
    app.register_blueprint(pages_bp)  # last — has the static catch-all

    return app
