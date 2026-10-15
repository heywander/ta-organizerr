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
    channel_id TEXT,
    published TEXT,
    symlink TEXT,
    status TEXT,
    is_live BOOLEAN DEFAULT 0,
    adopted_real BOOLEAN DEFAULT 0,
    video_codec TEXT,
    audio_codec TEXT,
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
CREATE TABLE IF NOT EXISTS channel_identities (
    channel_id TEXT PRIMARY KEY,
    display_name TEXT,
    provenance TEXT NOT NULL DEFAULT 'source',
    storage_component TEXT,
    designation TEXT NOT NULL DEFAULT 'none',
    designation_confirmed_at TIMESTAMP,
    designation_confirmed_by TEXT,
    current_state TEXT NOT NULL DEFAULT 'unknown',
    current_reason_code TEXT,
    current_state_at TIMESTAMP,
    last_accessible_at TIMESTAMP,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS video_availability (
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    current_state TEXT NOT NULL DEFAULT 'unknown',
    current_reason_code TEXT,
    current_state_at TIMESTAMP,
    last_accessible_at TIMESTAMP,
    last_observation_id INTEGER,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_video_availability_channel
    ON video_availability(channel_id, current_state, video_id);
CREATE TABLE IF NOT EXISTS availability_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT 0,
    payload_json TEXT,
    error TEXT,
    message TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_availability_jobs_channel
    ON availability_jobs(channel_id, created_at);
CREATE TABLE IF NOT EXISTS availability_job_subjects (
    job_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    source TEXT NOT NULL DEFAULT 'inventory',
    attempts INTEGER NOT NULL DEFAULT 0,
    observation_id INTEGER,
    error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_availability_job_subjects_progress
    ON availability_job_subjects(job_id, status, ordinal);
CREATE TABLE IF NOT EXISTS availability_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    normalized_state TEXT NOT NULL,
    reason_code TEXT,
    provider TEXT NOT NULL,
    raw_reason TEXT,
    evidence_json TEXT,
    tool_version TEXT,
    observed_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_availability_observation_subject
    ON availability_observations(subject_type, subject_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_availability_observation_job
    ON availability_observations(job_id, observation_id);
CREATE INDEX IF NOT EXISTS idx_availability_observation_channel_timeline
    ON availability_observations(
        subject_type, channel_id, observed_at, observation_id, normalized_state
    );
CREATE TABLE IF NOT EXISTS availability_designation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_availability_designation_events_channel
    ON availability_designation_events(channel_id, event_id);
CREATE TABLE IF NOT EXISTS video_designations (
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    designation TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    original_component TEXT NOT NULL,
    storage_component TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    confirmed_at TIMESTAMP NOT NULL,
    confirmed_by TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_video_designations_channel
    ON video_designations(channel_id, designation, video_id);
CREATE TABLE IF NOT EXISTS video_designation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_video_designation_events_channel
    ON video_designation_events(channel_id, event_id);
CREATE TABLE IF NOT EXISTS video_designation_plans (
    token_hash TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recovered_channel_cases (
    case_id TEXT PRIMARY KEY,
    source_channel_id TEXT NOT NULL UNIQUE,
    target_channel_name TEXT NOT NULL,
    target_component TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recovered_channel_items (
    item_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    suffix TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    sha256 TEXT,
    inspection_status TEXT NOT NULL DEFAULT 'pending',
    health_tier TEXT,
    diagnosis_json TEXT,
    expected_duration_seconds REAL,
    decision TEXT NOT NULL DEFAULT 'pending',
    organized_path TEXT,
    organized_at TIMESTAMP,
    error TEXT,
    manual_title TEXT,
    manual_published TEXT,
    metadata_source_url TEXT,
    metadata_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(case_id, source_path)
);
CREATE INDEX IF NOT EXISTS idx_recovered_items_case
    ON recovered_channel_items(case_id, inspection_status, health_tier, decision);
CREATE INDEX IF NOT EXISTS idx_recovered_items_video
    ON recovered_channel_items(video_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovered_one_approved_candidate
    ON recovered_channel_items(case_id, video_id)
    WHERE decision = 'approved';
CREATE TABLE IF NOT EXISTS recovered_channel_jobs (
    job_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT 0,
    payload_json TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recovered_jobs_case
    ON recovered_channel_jobs(case_id, created_at);
CREATE TABLE IF NOT EXISTS recovered_channel_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    item_id TEXT,
    actor TEXT,
    event TEXT NOT NULL,
    details_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recovered_events_case
    ON recovered_channel_events(case_id, event_id);
CREATE TABLE IF NOT EXISTS recovered_channel_attach_plans (
    token_hash TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    target_component TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recovered_attach_plans_expiry
    ON recovered_channel_attach_plans(expires_at, consumed_at);
CREATE TABLE IF NOT EXISTS recovered_media_repairs (
    repair_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    damaged_item_id TEXT NOT NULL,
    reference_item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    source_sha256 TEXT,
    reference_sha256 TEXT,
    candidate_sha256 TEXT,
    source_fingerprint_json TEXT,
    reference_fingerprint_json TEXT,
    before_json TEXT,
    after_json TEXT,
    assessment TEXT,
    candidate_path TEXT,
    backup_path TEXT,
    backup_sha256 TEXT,
    decision TEXT NOT NULL DEFAULT 'pending',
    cancel_requested BOOLEAN NOT NULL DEFAULT 0,
    log_json TEXT,
    tool_version TEXT,
    action_actor TEXT,
    error TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    adopted_at TIMESTAMP,
    restored_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_recovered_repairs_case
    ON recovered_media_repairs(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_recovered_repairs_damaged
    ON recovered_media_repairs(damaged_item_id, created_at);
CREATE TABLE IF NOT EXISTS recovered_media_repair_tokens (
    token_hash TEXT PRIMARY KEY,
    repair_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at TIMESTAMP
);
"""

_MIGRATIONS = [
    "ALTER TABLE videos ADD COLUMN is_live BOOLEAN DEFAULT 0",
    "ALTER TABLE videos ADD COLUMN published TEXT",
    "ALTER TABLE videos ADD COLUMN adopted_real BOOLEAN DEFAULT 0",
    "ALTER TABLE videos ADD COLUMN channel_id TEXT",
    "ALTER TABLE availability_jobs ADD COLUMN phase TEXT NOT NULL DEFAULT 'queued'",
    "ALTER TABLE availability_jobs ADD COLUMN message TEXT",
    "ALTER TABLE recovered_media_repairs ADD COLUMN tool_version TEXT",
    "ALTER TABLE recovered_media_repairs ADD COLUMN action_actor TEXT",
    "ALTER TABLE videos ADD COLUMN video_codec TEXT",
    "ALTER TABLE videos ADD COLUMN audio_codec TEXT",
]


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def stash_codecs(conn: sqlite3.Connection) -> None:
    """Park probed codecs in a temp table before a scan rewrites ``videos``.

    A full scan deletes and re-inserts the linked rows, which would otherwise
    throw away every ffprobe result and force the Transcode page to re-probe
    the whole library. Pair with :func:`restore_codecs` on the same connection.
    """
    conn.execute("DROP TABLE IF EXISTS temp.codec_cache")
    conn.execute(
        "CREATE TEMP TABLE codec_cache AS "
        "SELECT video_id, video_codec, audio_codec FROM videos "
        "WHERE video_codec IS NOT NULL OR audio_codec IS NOT NULL"
    )


def restore_codecs(conn: sqlite3.Connection) -> None:
    """Copy codecs stashed by :func:`stash_codecs` back onto the new rows."""
    conn.execute(
        """
        UPDATE videos SET
            video_codec = (SELECT c.video_codec FROM temp.codec_cache c WHERE c.video_id = videos.video_id),
            audio_codec = (SELECT c.audio_codec FROM temp.codec_cache c WHERE c.video_id = videos.video_id)
        WHERE video_id IN (SELECT video_id FROM temp.codec_cache)
        """
    )
    conn.execute("DROP TABLE IF EXISTS temp.codec_cache")


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


def _migrate_video_availability_key(conn: sqlite3.Connection) -> None:
    """Bind video state to channel ownership on databases from the first draft."""
    columns = conn.execute("PRAGMA table_info(video_availability)").fetchall()
    primary_key = [
        row["name"]
        for row in sorted((row for row in columns if row["pk"]), key=lambda row: row["pk"])
    ]
    if primary_key == ["channel_id", "video_id"]:
        return
    conn.execute("DROP INDEX IF EXISTS idx_video_availability_channel")
    conn.execute("ALTER TABLE video_availability RENAME TO video_availability_legacy")
    conn.execute(
        """
        CREATE TABLE video_availability (
            channel_id TEXT NOT NULL,
            video_id TEXT NOT NULL,
            current_state TEXT NOT NULL DEFAULT 'unknown',
            current_reason_code TEXT,
            current_state_at TIMESTAMP,
            last_accessible_at TIMESTAMP,
            last_observation_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel_id, video_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO video_availability
            (channel_id, video_id, current_state, current_reason_code,
             current_state_at, last_accessible_at, last_observation_id, updated_at)
        SELECT channel_id, video_id, current_state, current_reason_code,
               current_state_at, last_accessible_at, last_observation_id, updated_at
        FROM video_availability_legacy
        WHERE channel_id IS NOT NULL AND video_id IS NOT NULL
        """
    )
    conn.execute("DROP TABLE video_availability_legacy")
    conn.execute(
        "CREATE INDEX idx_video_availability_channel "
        "ON video_availability(channel_id, current_state, video_id)"
    )


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
        _migrate_video_availability_key(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_channel_id "
            "ON videos(channel_id, video_id)"
        )
        # Replace the first availability index layout, whose state column
        # prevented SQLite from streaming a channel timeline in timestamp
        # order.  The new prefix serves channel aggregates and ordered
        # eligibility scans without a temporary sort.
        conn.execute("DROP INDEX IF EXISTS idx_availability_observation_channel_state")
        timeline_columns = [
            item[2]
            for item in conn.execute(
                "PRAGMA index_info(idx_availability_observation_channel_timeline)"
            )
        ]
        expected_timeline_columns = [
            "subject_type",
            "channel_id",
            "observed_at",
            "observation_id",
            "normalized_state",
        ]
        if timeline_columns and timeline_columns != expected_timeline_columns:
            conn.execute(
                "DROP INDEX idx_availability_observation_channel_timeline"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_availability_observation_channel_timeline
            ON availability_observations(
                subject_type, channel_id, observed_at,
                observation_id, normalized_state
            )
            """
        )
        # Versions before the active-job uniqueness invariant could race two
        # queued jobs into one case. Normalize such rows before adding the
        # partial index or an upgrade would fail on every startup. The newest
        # inserted row remains resumable; older active rows become durable,
        # explicitly interrupted history.
        conn.execute(
            """
            UPDATE recovered_channel_jobs
            SET status = 'interrupted',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                error = COALESCE(
                    error,
                    'Interrupted during upgrade: a newer active case job superseded this row'
                )
            WHERE status IN ('queued', 'running', 'cancelling')
              AND rowid NOT IN (
                  SELECT MAX(rowid)
                  FROM recovered_channel_jobs
                  WHERE status IN ('queued', 'running', 'cancelling')
                  GROUP BY case_id
              )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recovered_one_active_job
            ON recovered_channel_jobs(case_id)
            WHERE status IN ('queued', 'running', 'cancelling')
            """
        )
        # Availability workers are process-local. Active rows from a prior
        # process remain useful history, but must not masquerade as running.
        conn.execute(
            """
            UPDATE availability_jobs
            SET status = 'interrupted',
                phase = 'interrupted',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                error = COALESCE(error, 'Server restarted before the availability check completed')
            WHERE status IN ('queued', 'running', 'cancelling')
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_availability_one_active_channel_job
            ON availability_jobs(channel_id)
            WHERE status IN ('queued', 'running', 'cancelling')
            """
        )
        # Existing recovered cases already provide a canonical channel ID and
        # a stable storage component. Preserve that ownership on upgrade.
        conn.execute(
            """
            INSERT INTO channel_identities
                (channel_id, display_name, provenance, storage_component)
            SELECT source_channel_id, target_channel_name, 'recovered', target_component
            FROM recovered_channel_cases
            WHERE 1
            ON CONFLICT(channel_id) DO UPDATE SET
                display_name = COALESCE(channel_identities.display_name, excluded.display_name),
                provenance = CASE
                    WHEN channel_identities.provenance = 'source' THEN 'recovered'
                    ELSE channel_identities.provenance
                END,
                storage_component = COALESCE(
                    channel_identities.storage_component,
                    excluded.storage_component
                ),
                updated_at = CURRENT_TIMESTAMP
            """
        )
        conn.execute(
            """
            UPDATE videos
            SET channel_id = (
                SELECT cases.source_channel_id
                FROM recovered_channel_items AS items
                JOIN recovered_channel_cases AS cases USING (case_id)
                WHERE items.video_id = videos.video_id
                  AND items.organized_path = videos.symlink
                LIMIT 1
            )
            WHERE videos.channel_id IS NULL
              AND videos.status = 'recovered'
            """
        )
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
