"""Authentication: first-run setup, login, session, persistent secret key."""
from __future__ import annotations

import secrets
from functools import wraps
from typing import Optional

from flask import jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .logging_util import log


_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = 256
_MAX_USERNAME_LENGTH = 64


class AdminAlreadyExists(RuntimeError):
    """Raised when first-run setup loses a race to another request."""


def admin_exists() -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return row is not None


def get_or_create_secret_key(env_value: Optional[str]) -> str:
    """Return the env-supplied secret if present, otherwise persist a random one."""
    if env_value:
        return env_value
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM app_config WHERE key = 'secret_key'").fetchone()
        if row:
            conn.commit()
            return row["value"]
        new_key = secrets.token_urlsafe(48)
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES ('secret_key', ?)",
            (new_key,),
        )
        conn.commit()
    log("🔐 Generated and persisted new FLASK_SECRET_KEY (no env var supplied).")
    return new_key


def validate_credentials(username: str, password: str) -> Optional[str]:
    """Return an error message if the inputs are unacceptable, else None."""
    if not username or not isinstance(username, str):
        return "Username is required"
    if len(username) > _MAX_USERNAME_LENGTH:
        return f"Username must be at most {_MAX_USERNAME_LENGTH} characters"
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return "Username may only contain letters, digits, '_', '-', '.'"
    if not password or not isinstance(password, str):
        return "Password is required"
    if len(password) < _MIN_PASSWORD_LENGTH:
        return f"Password must be at least {_MIN_PASSWORD_LENGTH} characters"
    if len(password) > _MAX_PASSWORD_LENGTH:
        return f"Password must be at most {_MAX_PASSWORD_LENGTH} characters"
    return None


def create_admin(username: str, password: str) -> None:
    password_hash = generate_password_hash(password)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            conn.rollback()
            raise AdminAlreadyExists("Setup already complete")
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        conn.commit()
    log(f"👤 Admin user created: {username}")


def check_login(username: str, password: str) -> bool:
    if not username or not password:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        # Run hash check against a dummy to avoid leaking timing.
        check_password_hash(
            "pbkdf2:sha256:600000$dummy$0000000000000000000000000000000000000000000000000000000000000000",
            password,
        )
        return False
    return check_password_hash(row["password_hash"], password)


def requires_auth(view):
    """Reject unauthenticated requests; redirect or 401 depending on path."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return view(*args, **kwargs)

    return wrapper
