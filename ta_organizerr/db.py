"""SQLite connection management and schema bootstrap."""
from __future__ import annotations

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
]


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    log(f"DB migration warning: {exc}")
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
