"""SQLite connection management and schema bootstrap."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH
from .logging_util import log


_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    channel TEXT,
    published TEXT,
    symlink TEXT,
    status TEXT,
    is_live BOOLEAN DEFAULT 0,
    adopted_real BOOLEAN DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lost_media (
    video_id TEXT PRIMARY KEY,
    filepath TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hidden_channels (
    channel_name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN is_live BOOLEAN DEFAULT 0",
    "ALTER TABLE videos ADD COLUMN published TEXT",
    "ALTER TABLE videos ADD COLUMN adopted_real BOOLEAN DEFAULT 0",
]


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _prepare_db_path() -> None:
    """Create private state paths before SQLite or secrets touch them."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        DB_PATH.parent.chmod(0o700)
    except OSError as exc:
        log(f"DB security warning: could not chmod {DB_PATH.parent} to 0700: {exc}")

    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(DB_PATH, flags, 0o600)
    os.close(fd)
    try:
        DB_PATH.chmod(0o600)
    except OSError as exc:
        log(f"DB security warning: could not chmod {DB_PATH} to 0600: {exc}")


def init_db() -> None:
    _prepare_db_path()
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


def init_db_with_retry(max_attempts: int = 30, delay_seconds: int = 10) -> None:
    """Init DB, retrying on transient errors (e.g. SMB lock).

    Bounded so we fail loudly instead of looping forever.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            init_db()
            print("Database initialized successfully.", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 — startup must surface anything
            if attempt == max_attempts:
                print(
                    f"CRITICAL: Database initialization failed after {max_attempts} attempts: {exc}",
                    flush=True,
                )
                sys.exit(1)
            print(
                f"Database initialization failed (attempt {attempt}/{max_attempts}, retrying in {delay_seconds}s): {exc}",
                flush=True,
            )
            time.sleep(delay_seconds)
