"""Conservative, evidence-first YouTube channel and video availability checks.

Online responses are observations, not filesystem instructions. This module
never renames, moves, deletes, or rewrites archive media. In particular, a
generic missing result is not evidence that a channel was terminated or that a
video was removed for a particular reason.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests

from .config import (
    AVAILABILITY_MAX_CONCURRENT_JOBS,
    AVAILABILITY_PROBE_DELAY_SECONDS,
    AVAILABILITY_PROBE_RETRIES,
    AVAILABILITY_PROBE_TIMEOUT_SECONDS,
    AVAILABILITY_RETRY_BACKOFF_SECONDS,
    HIDDEN_DIR,
    MAX_PER_PAGE,
    SOURCE_DIR,
    TARGET_DIR,
    YOUTUBE_API_KEY,
    YTDLP_BINARY,
)
from .db import get_db
from .logging_util import log
from .utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    is_video,
    path_in_allowed_tree,
    path_within,
    resolve_media_source,
)


_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_ACTIVE_STATES = {"queued", "running", "cancelling"}
_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted", "paused"}
_CHANNEL_STATES = {
    "accessible",
    "terminated_explicit",
    "restricted",
    "unavailable_unknown",
    "transient_failure",
    "conflicting_evidence",
}
_VIDEO_STATES = {
    "available",
    "private",
    "community_guidelines_removed",
    "other_policy_removed",
    "uploader_removed",
    "account_terminated",
    "restricted",
    "live_processing",
    "unavailable_unknown",
    "transient_failure",
}
_MAX_RAW_REASON = 4000
_MAX_EVIDENCE_JSON = 16_000
_MAX_VIDEO_JOB_SUBJECTS = 10_000
_TERMINATION_CONFIRMATION_INTERVAL = timedelta(hours=24)
_CLASSIFIER_VERSION = "availability-v2"
_SOURCE_INVENTORY_CACHE_SECONDS = 30.0
_LOCAL_INVENTORY_CACHE_SECONDS = 30.0
_MAX_ELIGIBILITY_DIAGNOSTIC_OBSERVATIONS = 10

_COMMUNITY_GUIDELINES_CHANNEL_REMOVAL_RE = re.compile(
    r"^(?:this|the|your)\s+(?:channel|account)\s+"
    r"(?:was|has\s+been)\s+removed\s+(?:because\s+it\s+|for\s+)?"
    r"violat(?:ed|ing)\s+(?:our|youtube(?:['\N{RIGHT SINGLE QUOTATION MARK}]s)?)\s+"
    r"community\s+guidelines[.!]?$",
    re.IGNORECASE,
)
_EXPLICIT_CHANNEL_ENFORCEMENT_RE = re.compile(
    r"^(?:this|the|your)\s+(?:youtube\s+)?(?:channel|account)\s+"
    r"(?:(?:has|had)\s+been|was|is)\s+"
    r"(?P<action>terminated|suspended)\b(?:\s+.*)?[.!]?$",
    re.IGNORECASE,
)

_runtime_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}
_job_guard = threading.BoundedSemaphore(AVAILABILITY_MAX_CONCURRENT_JOBS)
_source_inventory_lock = threading.RLock()
_source_inventory_cache: dict[
    tuple[str, str], tuple[float, dict[str, dict[str, Any]]]
] = {}
_local_inventory_cache: dict[
    tuple[str, str, str, str], tuple[float, dict[str, dict[str, Any]]]
] = {}
_inventory_key_locks: dict[tuple[str, ...], threading.Lock] = {}


class AvailabilityError(RuntimeError):
    """An operator-facing availability workflow error."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate_channel_id(value: object) -> str:
    if not isinstance(value, str) or _CHANNEL_ID_RE.fullmatch(value) is None:
        raise AvailabilityError("A canonical 24-character UC channel ID is required")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _evidence_json(value: object) -> str:
    encoded = _json(value)
    if len(encoded) <= _MAX_EVIDENCE_JSON:
        return encoded
    return _json({"truncated": True, "summary": "Evidence exceeded the storage limit"})


def _decode_json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def upsert_channel_identity(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    display_name: str | None = None,
    provenance: str = "source",
    storage_component: str | None = None,
) -> None:
    """Register exact channel ownership without changing availability evidence."""
    channel_id = validate_channel_id(channel_id)
    if provenance not in {"source", "normal", "recovered"}:
        raise ValueError("Unknown channel identity provenance")
    name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else None
    component = (
        storage_component.strip()
        if isinstance(storage_component, str) and storage_component.strip()
        else None
    )
    conn.execute(
        """
        INSERT INTO channel_identities
            (channel_id, display_name, provenance, storage_component)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            display_name = CASE
                WHEN channel_identities.provenance = 'recovered'
                     AND excluded.provenance != 'recovered'
                THEN channel_identities.display_name
                ELSE COALESCE(excluded.display_name, channel_identities.display_name)
            END,
            provenance = CASE
                WHEN channel_identities.provenance = 'recovered' THEN 'recovered'
                WHEN excluded.provenance = 'recovered' THEN 'recovered'
                WHEN channel_identities.provenance = 'normal' THEN 'normal'
                ELSE excluded.provenance
            END,
            storage_component = COALESCE(
                excluded.storage_component,
                channel_identities.storage_component
            ),
            updated_at = CURRENT_TIMESTAMP
        """,
        (channel_id, name, provenance, component),
    )


def upsert_metadata_identities(conn: sqlite3.Connection, video_map: dict[str, dict]) -> None:
    """Persist canonical channel identities exposed by TubeArchivist metadata."""
    seen: set[str] = set()
    for metadata in video_map.values():
        if not isinstance(metadata, dict):
            continue
        channel_id = metadata.get("channel_id")
        if not isinstance(channel_id, str) or _CHANNEL_ID_RE.fullmatch(channel_id) is None:
            continue
        if channel_id not in seen:
            upsert_channel_identity(
                conn,
                channel_id,
                display_name=str(metadata.get("channel_name") or channel_id),
                provenance="normal",
            )
            seen.add(channel_id)


def _discover_source_identities() -> None:
    """Register immediate canonical source roots; never walk their media here."""
    try:
        root_stat = os.lstat(SOURCE_DIR)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            return
        entries = list(SOURCE_DIR.iterdir())
    except OSError:
        return
    channel_ids: list[str] = []
    for entry in entries:
        if _CHANNEL_ID_RE.fullmatch(entry.name) is None:
            continue
        try:
            entry_stat = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
            channel_ids.append(entry.name)
    if not channel_ids:
        return
    with get_db() as conn:
        for channel_id in channel_ids:
            upsert_channel_identity(conn, channel_id, provenance="source")
        conn.commit()


def _channel_row(
    row: sqlite3.Row,
    local_video_count: int = 0,
    active_job_id: str | None = None,
    observation_count: int = 0,
    eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    channel_id = row["channel_id"]
    eligibility = eligibility or termination_eligibility(channel_id)
    return {
        "channel_id": channel_id,
        "display_name": row["display_name"] or channel_id,
        "provenance": row["provenance"],
        "storage_component": row["storage_component"],
        "designation": row["designation"],
        "designation_confirmed_at": row["designation_confirmed_at"],
        "designation_confirmed_by": row["designation_confirmed_by"],
        "current_state": row["current_state"],
        "current_reason_code": row["current_reason_code"],
        "current_state_at": row["current_state_at"],
        "last_accessible_at": row["last_accessible_at"],
        "revision": int(row["revision"] or 0),
        "termination_candidate": eligibility["eligible"],
        "termination_eligibility": eligibility,
        "local_video_count": int(local_video_count),
        "active_job_id": active_job_id,
        "observation_count": int(observation_count),
    }


def list_channels() -> list[dict[str, Any]]:
    _discover_source_identities()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_identities ORDER BY COALESCE(display_name, channel_id), channel_id"
        ).fetchall()
        local_counts = {
            row["channel_id"]: int(row["count"])
            for row in conn.execute(
                """
                SELECT channel_id, COUNT(*) AS count
                FROM (
                    SELECT channel_id, video_id
                    FROM videos WHERE channel_id IS NOT NULL
                    UNION
                    SELECT cases.source_channel_id AS channel_id, items.video_id
                    FROM recovered_channel_items AS items
                    JOIN recovered_channel_cases AS cases USING (case_id)
                )
                GROUP BY channel_id
                """
            )
        }
        active_jobs = {
            row["channel_id"]: row["job_id"]
            for row in conn.execute(
                """
                SELECT channel_id, job_id FROM availability_jobs
                WHERE status IN ('queued', 'running', 'cancelling')
                ORDER BY created_at DESC
                """
            )
        }
        observation_counts = {
            row["channel_id"]: int(row["count"])
            for row in conn.execute(
                """
                SELECT channel_id, COUNT(*) AS count
                FROM availability_observations
                WHERE subject_type = 'channel'
                GROUP BY channel_id
                """
            )
        }
        channel_observations: dict[str, list[sqlite3.Row]] = {}
        for observation in conn.execute(
            """
            SELECT observation_id, channel_id, normalized_state, reason_code,
                   observed_at
            FROM availability_observations
            WHERE subject_type = 'channel'
              AND normalized_state IN (
                  'terminated_explicit', 'accessible', 'conflicting_evidence'
              )
            ORDER BY channel_id, observed_at, observation_id
            """
        ):
            channel_observations.setdefault(observation["channel_id"], []).append(
                observation
            )
    return [
        _channel_row(
            row,
            local_counts.get(row["channel_id"], 0),
            active_jobs.get(row["channel_id"]),
            observation_counts.get(row["channel_id"], 0),
            _termination_eligibility_from_rows(
                row["current_state"],
                row["current_reason_code"],
                channel_observations.get(row["channel_id"], []),
            ),
        )
        for row in rows
    ]


def get_channel(channel_id: str) -> dict[str, Any]:
    channel_id = validate_channel_id(channel_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_identities WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            raise AvailabilityError("Channel identity not found", 404)
        observations = conn.execute(
            """
            SELECT observation_id, job_id, normalized_state, reason_code, provider,
                   raw_reason, evidence_json, tool_version, observed_at
            FROM availability_observations
            WHERE subject_type = 'channel' AND subject_id = ?
            ORDER BY observed_at DESC, observation_id DESC LIMIT 50
            """,
            (channel_id,),
        ).fetchall()
        observation_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM availability_observations
            WHERE subject_type = 'channel' AND subject_id = ?
            """,
            (channel_id,),
        ).fetchone()["count"]
        active_job = conn.execute(
            """
            SELECT job_id FROM availability_jobs
            WHERE channel_id = ? AND status IN ('queued', 'running', 'cancelling')
            ORDER BY created_at DESC LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
    result = _channel_row(
        row,
        len(_local_inventory(channel_id)),
        active_job["job_id"] if active_job is not None else None,
        observation_count=int(observation_count),
    )
    result["observations"] = [
        {
            "observation_id": item["observation_id"],
            "job_id": item["job_id"],
            "state": item["normalized_state"],
            "reason_code": item["reason_code"],
            "provider": item["provider"],
            "raw_reason": item["raw_reason"],
            "evidence": _decode_json(item["evidence_json"], {}),
            "tool_version": item["tool_version"],
            "observed_at": item["observed_at"],
        }
        for item in observations
    ]
    return result


def list_observations(
    channel_id: str,
    *,
    subject_type: str,
    subject_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    channel_id = validate_channel_id(channel_id)
    if subject_type not in {"channel", "video"}:
        raise AvailabilityError("subject_type must be channel or video")
    if subject_type == "channel":
        if subject_id not in (None, channel_id):
            raise AvailabilityError("A channel observation must use the selected channel ID")
        subject_id = channel_id
    elif subject_id is not None and not is_valid_youtube_id(subject_id):
        raise AvailabilityError("subject_id must be an exact 11-character video ID")
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        raise AvailabilityError("limit must be an integer") from None
    clauses = ["channel_id = ?", "subject_type = ?"]
    values: list[object] = [channel_id, subject_type]
    if subject_id is not None:
        clauses.append("subject_id = ?")
        values.append(subject_id)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT observation_id, job_id, subject_type, subject_id,
                   normalized_state, reason_code, provider, raw_reason,
                   evidence_json, tool_version, observed_at
            FROM availability_observations
            WHERE {' AND '.join(clauses)}
            ORDER BY observed_at DESC, observation_id DESC LIMIT ?
            """,
            (*values, limit),
        ).fetchall()
    return [
        {
            "observation_id": row["observation_id"],
            "job_id": row["job_id"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "normalized_state": row["normalized_state"],
            "reason_code": row["reason_code"],
            "provider": row["provider"],
            "raw_reason": row["raw_reason"],
            "evidence": _decode_json(row["evidence_json"], {}),
            "tool_version": row["tool_version"],
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]


def _safe_preview_path(
    value: object,
    *,
    expected_video_id: str | None = None,
    allow_idless_real: bool = False,
) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value)
    if not is_video(path) or not path_in_allowed_tree(path, (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR)):
        return None
    try:
        if not os.path.lexists(path):
            return None
        mode = os.lstat(path).st_mode
    except OSError:
        return None
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        return None
    source = resolve_media_source(path)
    if source is None or not is_video(source):
        return None
    if not path_within(source, (SOURCE_DIR, TARGET_DIR, HIDDEN_DIR)):
        return None
    try:
        source_stat = os.lstat(source)
    except OSError:
        return None
    # resolve_media_source intentionally translates exactly one app-owned link.
    # Refuse a second filesystem link instead of accidentally validating the
    # intermediate filename while a different terminal file is previewed.
    if stat.S_ISLNK(source_stat.st_mode):
        return None
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 0:
        return None
    if expected_video_id is not None:
        entry_id = extract_id_from_filename(path.name)
        source_id = extract_id_from_filename(source.name)
        if entry_id is not None and entry_id != expected_video_id:
            return None
        # A symlink's target is independent filesystem state. Require its
        # filename to carry the same exact video ID before offering it as the
        # selected ID's preview. Only an explicitly adopted DB real-file row
        # may use an ID-less legacy name such as video.mp4.
        if stat.S_ISLNK(mode) and source_id != expected_video_id:
            return None
        if source_id is not None and source_id != expected_video_id:
            return None
        if (
            stat.S_ISREG(mode)
            and entry_id is None
            and source_id is None
            and not allow_idless_real
        ):
            return None
    return str(path)


def _scan_source_inventory(channel_id: str) -> dict[str, dict[str, Any]]:
    """Walk one raw UCID tree without following directory symlinks."""

    inventory: dict[str, dict[str, Any]] = {}
    source_root = SOURCE_DIR / channel_id
    try:
        root_stat = os.lstat(source_root)
    except FileNotFoundError:
        return inventory
    except OSError:
        raise
    if root_stat is not None and stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISLNK(root_stat.st_mode):
        def raise_walk_error(error: OSError) -> None:
            raise error

        for current, dirs, files in os.walk(
            source_root,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            # Do not descend through directory symlinks on an archive mount.
            dirs[:] = [
                name for name in dirs
                if not os.path.islink(Path(current) / name)
            ]
            for name in files:
                video_id = extract_id_from_filename(name)
                if video_id is None:
                    continue
                candidate = Path(current) / name
                if not is_video(candidate):
                    continue
                try:
                    candidate_mode = os.lstat(candidate).st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(candidate_mode):
                    continue
                preview = _safe_preview_path(
                    str(candidate), expected_video_id=video_id
                )
                item = inventory.setdefault(
                    video_id,
                    {
                        "video_id": video_id,
                        "title": video_id,
                        "preview_path": None,
                        "path_kind": "source",
                    },
                )
                if preview is not None and item.get("preview_path") is None:
                    item["preview_path"] = preview
                    item["path_kind"] = "source"
    return inventory


def _source_inventory(
    channel_id: str,
    *,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    """Coalesce repeated UI searches so they do not re-walk a NAS tree.

    The cache contains only source-derived rows. Database-backed tracked and
    recovered records are merged fresh on every request. A real video-check
    job forces a new walk before it freezes its exact-ID subject list.
    """

    key = (str(SOURCE_DIR), channel_id)
    now = time.monotonic()
    with _source_inventory_lock:
        cached = _source_inventory_cache.get(key)
        if (
            not force
            and cached is not None
            and now - cached[0] <= _SOURCE_INVENTORY_CACHE_SECONDS
        ):
            return {video_id: dict(item) for video_id, item in cached[1].items()}
        key_lock = _inventory_key_locks.setdefault(("source", *key), threading.Lock())
    with key_lock:
        # A same-channel request may have populated the cache while this caller
        # waited. Different channels never wait on this network-tree walk.
        with _source_inventory_lock:
            cached = _source_inventory_cache.get(key)
            if (
                not force
                and cached is not None
                and time.monotonic() - cached[0]
                <= _SOURCE_INVENTORY_CACHE_SECONDS
            ):
                return {
                    video_id: dict(item)
                    for video_id, item in cached[1].items()
                }
        try:
            scanned = _scan_source_inventory(channel_id)
        except OSError as exc:
            raise AvailabilityError(
                f"Could not read the complete source inventory for {channel_id}: {exc}",
                503,
            ) from exc
        with _source_inventory_lock:
            _source_inventory_cache[key] = (time.monotonic(), scanned)
        return {video_id: dict(item) for video_id, item in scanned.items()}


def _clear_source_inventory_cache_for_tests() -> None:
    with _source_inventory_lock:
        _source_inventory_cache.clear()
        _local_inventory_cache.clear()


def _build_local_inventory(
    channel_id: str,
    *,
    force_source_scan: bool = False,
) -> dict[str, dict[str, Any]]:
    channel_id = validate_channel_id(channel_id)
    inventory: dict[str, dict[str, Any]] = {}
    with get_db() as conn:
        tracked = conn.execute(
            """
            SELECT video_id, title, symlink, status, adopted_real
            FROM videos WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchall()
        recovered = conn.execute(
            """
            SELECT items.video_id, items.source_path, items.organized_path,
                   items.manual_title
            FROM recovered_channel_items AS items
            JOIN recovered_channel_cases AS cases USING (case_id)
            WHERE cases.source_channel_id = ?
            """,
            (channel_id,),
        ).fetchall()
    for row in tracked:
        video_id = row["video_id"]
        if not is_valid_youtube_id(video_id):
            continue
        inventory[video_id] = {
            "video_id": video_id,
            "title": row["title"],
            "preview_path": _safe_preview_path(
                row["symlink"],
                expected_video_id=video_id,
                allow_idless_real=bool(row["adopted_real"]),
            ),
            "path_kind": "organized",
        }
    for row in recovered:
        video_id = row["video_id"]
        if not is_valid_youtube_id(video_id):
            continue
        preview = _safe_preview_path(
            row["source_path"], expected_video_id=video_id
        ) or _safe_preview_path(
            row["organized_path"], expected_video_id=video_id
        )
        current = inventory.setdefault(
            video_id,
            {"video_id": video_id, "title": row["manual_title"] or video_id, "preview_path": None, "path_kind": "recovered"},
        )
        if preview:
            current["preview_path"] = preview
            current["path_kind"] = "recovered_source"

    for video_id, source_item in _source_inventory(
        channel_id, force=force_source_scan
    ).items():
        item = inventory.setdefault(video_id, source_item)
        if item.get("preview_path") is None:
            item["preview_path"] = source_item["preview_path"]
            item["path_kind"] = "source"
    return inventory


def _local_inventory(
    channel_id: str,
    *,
    force_source_scan: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return a coalesced, validated snapshot of all local channel media.

    Validating thousands of persisted paths can be as expensive as walking the
    raw source tree when the archive is on a network mount. Cache that complete
    structural snapshot—not merely the source walk—so search, filtering, and
    pagination do not repeat resolve/stat calls. A real check job always forces
    a new snapshot before freezing its exact subject IDs.
    """

    channel_id = validate_channel_id(channel_id)
    key = (str(SOURCE_DIR), str(TARGET_DIR), str(HIDDEN_DIR), channel_id)
    now = time.monotonic()
    with _source_inventory_lock:
        cached = _local_inventory_cache.get(key)
        if (
            not force_source_scan
            and cached is not None
            and now - cached[0] <= _LOCAL_INVENTORY_CACHE_SECONDS
        ):
            return cached[1]
        key_lock = _inventory_key_locks.setdefault(("local", *key), threading.Lock())
    with key_lock:
        with _source_inventory_lock:
            cached = _local_inventory_cache.get(key)
            if (
                not force_source_scan
                and cached is not None
                and time.monotonic() - cached[0]
                <= _LOCAL_INVENTORY_CACHE_SECONDS
            ):
                return cached[1]
        inventory = _build_local_inventory(
            channel_id,
            force_source_scan=force_source_scan,
        )
        with _source_inventory_lock:
            _local_inventory_cache[key] = (time.monotonic(), inventory)
        return inventory


def list_videos(
    channel_id: str,
    *,
    page: int = 1,
    per_page: int = 100,
    state: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    channel_id = validate_channel_id(channel_id)
    page = max(1, int(page))
    per_page = max(1, min(MAX_PER_PAGE, int(per_page)))
    state_aliases = {
        "unchecked": "unknown",
        "community_guidelines": "community_guidelines_removed",
        "policy_removed": "other_policy_removed",
        "transient": "transient_failure",
    }
    if state == "all":
        state = None
    if state in state_aliases:
        state = state_aliases[state]
    if state and state not in _VIDEO_STATES | {"unknown"}:
        raise AvailabilityError("Unknown video availability state")
    if query is not None and not isinstance(query, str):
        raise AvailabilityError("q must be text")
    query = (query or "").strip()
    if len(query) > 200:
        raise AvailabilityError("q must be at most 200 characters")
    normalized_query = query.casefold()
    inventory = _local_inventory(channel_id)
    with get_db() as conn:
        states = {
            row["video_id"]: row
            for row in conn.execute(
                """
                SELECT availability.*, observations.raw_reason,
                       observations.evidence_json, observations.provider
                FROM video_availability AS availability
                LEFT JOIN availability_observations AS observations
                  ON observations.observation_id = availability.last_observation_id
                WHERE availability.channel_id = ?
                """,
                (channel_id,),
            )
        }
    items: list[dict[str, Any]] = []
    for video_id in sorted(inventory):
        item = dict(inventory[video_id])
        row = states.get(video_id)
        item.update({
            "state": row["current_state"] if row else "unknown",
            "reason_code": row["current_reason_code"] if row else None,
            "state_at": row["current_state_at"] if row else None,
            "last_accessible_at": row["last_accessible_at"] if row else None,
            "raw_reason": row["raw_reason"] if row else None,
            "evidence": _decode_json(row["evidence_json"], {}) if row else {},
            "provider": row["provider"] if row else None,
        })
        if state and item["state"] != state:
            continue
        if normalized_query:
            searchable = " ".join(
                str(item.get(key) or "")
                for key in ("video_id", "title", "raw_reason", "preview_path")
            ).casefold()
            if normalized_query not in searchable:
                continue
        items.append(item)
    total = len(items)
    offset = (page - 1) * per_page
    return {
        "videos": items[offset:offset + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page,
    }


@lru_cache(maxsize=1)
def _tool_version() -> str | None:
    try:
        completed = subprocess.run(
            [YTDLP_BINARY, "--ignore-config", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        version = completed.stdout.strip().splitlines()[0]
        if completed.returncode == 0 and version:
            return version[:120]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def _clean_reason(value: object) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    return text[-_MAX_RAW_REASON:]


def _yt_dlp_channel_message(reason: str, requested_channel_id: str | None) -> str | None:
    """Extract one exact-ID, line-anchored ``youtube:tab`` provider message."""
    if (
        not isinstance(requested_channel_id, str)
        or _CHANNEL_ID_RE.fullmatch(requested_channel_id) is None
    ):
        return None
    pattern = re.compile(
        r"^ERROR:\s*\[youtube:tab\]\s+"
        r"(?P<channel_id>UC[A-Za-z0-9_-]{22}):"
        r"\s*YouTube said:\s*(?P<message>[^\r\n]+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    # UCIDs are case-sensitive even though the fixed yt-dlp diagnostic text is
    # not. Compare the captured identifier outside the case-insensitive regex.
    matches = [
        match
        for match in pattern.finditer(reason)
        if match.group("channel_id") == requested_channel_id
    ]
    if not matches:
        return None
    return matches[-1].group("message").strip()


def _classify_failure(
    subject_type: str,
    reason: str,
    *,
    requested_subject_id: str | None = None,
) -> dict[str, Any]:
    lowered = reason.casefold()
    transient_markers = (
        "timed out", "temporary failure", "connection reset", "network is unreachable",
        "http error 500", "http error 502", "http error 503", "http error 504",
    )
    pause_markers = (
        "http error 429", "too many requests", "sign in to confirm you're not a bot",
        "confirm you’re not a bot", "captcha",
    )
    restricted_markers = (
        "sign in to confirm your age", "age-restricted", "not available in your country",
        "not available in your region", "blocked in your country", "blocked in your region",
        "geo-restricted", "not available from your location", "members-only",
        "members only", "login required",
    )
    if any(marker in lowered for marker in pause_markers):
        return {"state": "transient_failure", "reason_code": "rate_limited_or_challenged", "pause": True}
    if any(marker in lowered for marker in transient_markers):
        return {"state": "transient_failure", "reason_code": "network_or_service_failure", "retry": True}
    if any(marker in lowered for marker in restricted_markers):
        return {"state": "restricted", "reason_code": "access_restricted"}

    if subject_type == "channel":
        provider_message = _yt_dlp_channel_message(reason, requested_subject_id)
        enforcement = (
            _EXPLICIT_CHANNEL_ENFORCEMENT_RE.fullmatch(provider_message)
            if provider_message
            else None
        )
        if provider_message and (
            enforcement
            or _COMMUNITY_GUIDELINES_CHANNEL_REMOVAL_RE.fullmatch(provider_message)
        ):
            # The raw provider message retains the specific termination cause.
            # A stable reason family lets equivalent explicit wording satisfy
            # the repeated-evidence threshold without title-text heuristics.
            return {
                "state": "terminated_explicit",
                "reason_code": (
                    "channel_suspended"
                    if enforcement and enforcement.group("action").casefold() == "suspended"
                    else "channel_terminated"
                ),
            }
        return {"state": "unavailable_unknown", "reason_code": "channel_unavailable_unknown"}

    if "private video" in lowered or "video is private" in lowered:
        return {"state": "private", "reason_code": "video_private"}
    if "community guidelines" in lowered and any(
        marker in lowered for marker in ("removed", "violat", "strike")
    ):
        return {"state": "community_guidelines_removed", "reason_code": "community_guidelines"}
    if "copyright" in lowered and any(
        marker in lowered for marker in ("removed", "taken down", "takedown")
    ):
        return {"state": "other_policy_removed", "reason_code": "copyright"}
    if "terms of service" in lowered and any(marker in lowered for marker in ("removed", "violat")):
        return {"state": "other_policy_removed", "reason_code": "terms_of_service"}
    if "removed by the uploader" in lowered or "uploader has removed" in lowered:
        return {"state": "uploader_removed", "reason_code": "uploader_removed"}
    if "account associated with this video" in lowered and "terminated" in lowered:
        return {"state": "account_terminated", "reason_code": "source_account_terminated"}
    if any(marker in lowered for marker in ("premiere will begin", "live event will begin", "processing this video")):
        return {"state": "live_processing", "reason_code": "live_or_processing"}
    return {"state": "unavailable_unknown", "reason_code": "video_unavailable_unknown"}


def _classify_structured_success(
    subject_type: str,
    subject_id: str,
    parsed: dict[str, Any],
    returncode: int,
    expected_channel_id: str | None = None,
) -> dict[str, Any]:
    """Validate identity and yt-dlp's access marker before claiming access."""
    evidence = {
        "returncode": returncode,
        "extractor": parsed.get("extractor_key") or parsed.get("extractor"),
        "availability": parsed.get("availability"),
        "live_status": parsed.get("live_status"),
        "reported_id": parsed.get("id"),
        "channel_id": parsed.get("channel_id"),
    }
    if subject_type == "video":
        if parsed.get("id") != subject_id:
            return {
                "state": "transient_failure",
                "reason_code": "probe_identity_mismatch",
                "raw_reason": "yt-dlp did not return the exact requested video ID",
                "evidence": evidence,
            }
        reported_channel_id = parsed.get("channel_id")
        if (
            expected_channel_id is not None
            and isinstance(reported_channel_id, str)
            and _CHANNEL_ID_RE.fullmatch(reported_channel_id)
            and reported_channel_id != expected_channel_id
        ):
            return {
                "state": "transient_failure",
                "reason_code": "probe_channel_identity_mismatch",
                "raw_reason": "yt-dlp returned the video under a different canonical channel ID",
                "evidence": evidence,
            }
    else:
        # Channel/tab results vary in shape. Validate every canonical UCID the
        # extractor does provide, while allowing older shapes with none.
        reported_channel_ids = {
            value
            for value in (parsed.get("id"), parsed.get("channel_id"), parsed.get("uploader_id"))
            if isinstance(value, str) and _CHANNEL_ID_RE.fullmatch(value)
        }
        if any(value != subject_id for value in reported_channel_ids):
            return {
                "state": "transient_failure",
                "reason_code": "probe_identity_mismatch",
                "raw_reason": "yt-dlp returned metadata for a different canonical channel ID",
                "evidence": evidence,
            }
        if not reported_channel_ids:
            return {
                "state": "transient_failure",
                "reason_code": "probe_identity_unverified",
                "raw_reason": "yt-dlp returned channel metadata without a canonical channel ID",
                "evidence": evidence,
            }

    marker = str(parsed.get("availability") or "").strip().casefold()
    if subject_type == "video" and marker == "private":
        return {
            "state": "private", "reason_code": "video_private",
            "raw_reason": "yt-dlp returned metadata marked private", "evidence": evidence,
        }
    restricted_markers = {
        "needs_auth": "authentication_required",
        "premium_only": "premium_only",
        "subscriber_only": "subscriber_only",
    }
    if marker in restricted_markers or (subject_type == "channel" and marker == "private"):
        return {
            "state": "restricted",
            "reason_code": restricted_markers.get(marker, "private_channel_metadata"),
            "raw_reason": f"yt-dlp returned metadata marked {marker or 'restricted'}",
            "evidence": evidence,
        }
    live_status = str(parsed.get("live_status") or "").strip().casefold()
    if subject_type == "video" and live_status in {"is_upcoming", "post_live"}:
        return {
            "state": "live_processing",
            "reason_code": "upcoming" if live_status == "is_upcoming" else "processing",
            "raw_reason": (
                "yt-dlp reports that the video is upcoming"
                if live_status == "is_upcoming"
                else "yt-dlp reports that the completed live video is still processing"
            ),
            "evidence": evidence,
        }
    return {
        "state": "accessible" if subject_type == "channel" else "available",
        "reason_code": "public_response",
        "raw_reason": "Public metadata was returned",
        "evidence": evidence,
    }


def _yt_dlp_probe(
    subject_type: str,
    subject_id: str,
    *,
    expected_channel_id: str | None = None,
) -> dict[str, Any]:
    url = (
        f"https://www.youtube.com/channel/{subject_id}"
        if subject_type == "channel"
        else f"https://www.youtube.com/watch?v={subject_id}"
    )
    command = [
        YTDLP_BINARY,
        "--ignore-config",
        "--skip-download",
        "--simulate",
        "--dump-single-json",
        "--no-warnings",
        "--ignore-no-formats-error",
    ]
    if subject_type == "channel":
        # Inspect the channel page without processing its first video. A
        # private/restricted/broken upload is not evidence about channel state.
        command.extend(["--flat-playlist", "--playlist-end", "1"])
    command.append(url)
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=AVAILABILITY_PROBE_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "state": "transient_failure", "reason_code": "probe_timeout",
            "raw_reason": "yt-dlp availability probe timed out", "retry": True,
            "evidence": {"returncode": None},
        }
    except OSError as exc:
        return {
            "state": "transient_failure", "reason_code": "probe_unavailable",
            "raw_reason": f"Could not start yt-dlp: {exc.__class__.__name__}",
            "evidence": {"returncode": None},
        }

    stdout = completed.stdout.strip()
    if completed.returncode == 0 and stdout:
        try:
            parsed = json.loads(stdout.splitlines()[-1])
        except (TypeError, ValueError):
            return {
                "state": "transient_failure", "reason_code": "invalid_probe_output",
                "raw_reason": "yt-dlp returned malformed structured output",
                "retry": True, "evidence": {"returncode": completed.returncode},
            }
        if not isinstance(parsed, dict):
            return {
                "state": "transient_failure", "reason_code": "invalid_probe_output",
                "raw_reason": "yt-dlp returned an unexpected structured result",
                "retry": True, "evidence": {"returncode": completed.returncode},
            }
        return _classify_structured_success(
            subject_type,
            subject_id,
            parsed,
            completed.returncode,
            expected_channel_id,
        )
    reason = _clean_reason(completed.stderr or completed.stdout or "yt-dlp returned no public metadata")
    classified = _classify_failure(
        subject_type,
        reason,
        requested_subject_id=subject_id,
    )
    classified.update({"raw_reason": reason, "evidence": {"returncode": completed.returncode}})
    return classified


def _safe_api_error_reason(response: requests.Response) -> str | None:
    """Extract a bounded API reason without persisting request URLs or keys."""
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            value = item.get("reason")
            if (
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value.strip())
            ):
                return value.strip()
    # Free-form API messages can echo request details. Do not persist them.
    return None


def _api_probe(subject_type: str, subject_id: str) -> dict[str, Any] | None:
    if not YOUTUBE_API_KEY:
        return None
    endpoint = "channels" if subject_type == "channel" else "videos"
    try:
        response = requests.get(
            f"https://www.googleapis.com/youtube/v3/{endpoint}",
            params={"part": "id,status", "id": subject_id, "key": YOUTUBE_API_KEY},
            timeout=AVAILABILITY_PROBE_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        return {
            "state": "transient_failure", "reason_code": "api_network_failure",
            "raw_reason": f"YouTube Data API request failed: {exc.__class__.__name__}",
            "evidence": {},
        }
    if response.status_code == 429 or response.status_code >= 500:
        return {
            "state": "transient_failure", "reason_code": "api_rate_or_service_failure",
            "raw_reason": f"YouTube Data API returned HTTP {response.status_code}",
            "pause": response.status_code == 429,
            "evidence": {"status_code": response.status_code},
        }
    if response.status_code in {400, 401, 403}:
        safe_reason = _safe_api_error_reason(response)
        return {
            "state": "transient_failure",
            "reason_code": "api_forbidden" if response.status_code == 403 else "api_credentials_rejected",
            "raw_reason": (
                f"YouTube Data API disabled for this job after HTTP {response.status_code}"
                + (f" ({safe_reason})" if safe_reason else "")
            ),
            "disable_api": True,
            "evidence": {"status_code": response.status_code, "api_reason": safe_reason},
        }
    if response.status_code != 200:
        # Quota, permissions, and malformed-key errors cannot establish media state.
        return {
            "state": "transient_failure", "reason_code": "api_request_rejected",
            "raw_reason": f"YouTube Data API returned HTTP {response.status_code}",
            "evidence": {"status_code": response.status_code},
        }
    try:
        data = response.json()
    except ValueError:
        return {
            "state": "transient_failure", "reason_code": "api_invalid_response",
            "raw_reason": "YouTube Data API returned malformed JSON", "evidence": {},
        }
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(items, list) and items:
        returned_ids = [
            item.get("id") for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if subject_id not in returned_ids:
            return {
                "state": "transient_failure",
                "reason_code": "api_identity_mismatch",
                "raw_reason": "YouTube Data API returned a different resource ID",
                "evidence": {"item_count": len(items), "returned_ids": returned_ids[:10]},
            }
        return {
            "state": "accessible" if subject_type == "channel" else "available",
            "reason_code": "api_resource_returned",
            "raw_reason": "YouTube Data API returned the exact resource",
            "evidence": {"item_count": len(items), "returned_id": subject_id},
        }
    # A zero-item result deliberately stays unknown. The public API does not
    # reliably distinguish private/deleted/terminated resources.
    return {
        "state": "unavailable_unknown", "reason_code": "api_resource_absent",
        "raw_reason": "YouTube Data API did not return the exact resource",
        "evidence": {"item_count": 0},
    }


def _combine_probe(subject_type: str, ytdlp: dict[str, Any], api: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(ytdlp)
    provider = "yt-dlp"
    raw_ytdlp_reason = _clean_reason(ytdlp.get("raw_reason"))
    if api is not None:
        provider = "yt-dlp+youtube-data-api"
        if subject_type == "channel" and ytdlp["state"] == "terminated_explicit" and api["state"] == "accessible":
            result.update({
                "state": "conflicting_evidence",
                "reason_code": "api_reports_accessible",
                "raw_reason": (
                    f"{raw_ytdlp_reason}\n\n"
                    "Corroboration conflict: the Data API returned the exact channel."
                ).strip(),
            })
        if api.get("pause"):
            result["pause"] = True
        if api.get("disable_api"):
            result["disable_api"] = True
    result["provider"] = provider
    result["tool_version"] = _tool_version()
    ytdlp_evidence = dict(ytdlp.get("evidence") or {})
    if raw_ytdlp_reason:
        ytdlp_evidence["raw_reason"] = raw_ytdlp_reason
    result["evidence"] = {
        "classifier_version": _CLASSIFIER_VERSION,
        "yt_dlp": ytdlp_evidence,
        "youtube_api": api,
        "api_resource_exists": bool(
            api and api.get("state") in {"accessible", "available"}
        ),
    }
    return result


def probe(
    subject_type: str,
    subject_id: str,
    *,
    expected_channel_id: str | None = None,
    include_api: bool = True,
) -> dict[str, Any]:
    """Probe one exact public identifier with bounded transient retries."""
    latest: dict[str, Any] | None = None
    for attempt in range(1, AVAILABILITY_PROBE_RETRIES + 1):
        latest = _yt_dlp_probe(
            subject_type,
            subject_id,
            expected_channel_id=expected_channel_id,
        )
        if latest.get("pause") or not latest.get("retry"):
            break
        if attempt < AVAILABILITY_PROBE_RETRIES and AVAILABILITY_RETRY_BACKOFF_SECONDS:
            time.sleep(AVAILABILITY_RETRY_BACKOFF_SECONDS * attempt)
    assert latest is not None
    api = _api_probe(subject_type, subject_id) if include_api else None
    return _combine_probe(subject_type, latest, api)


def record_observation(
    job_id: str,
    subject_type: str,
    subject_id: str,
    channel_id: str,
    result: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> int:
    """Persist a normalized result and update its current aggregate."""
    observed_at = observed_at or _now()
    state = str(result.get("state") or "transient_failure")
    allowed = _CHANNEL_STATES if subject_type == "channel" else _VIDEO_STATES
    if state not in allowed:
        raise ValueError("Unknown normalized availability state")
    reason_code = str(result.get("reason_code") or "unspecified")[:160]
    raw_reason = _clean_reason(result.get("raw_reason"))
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO availability_observations
                (job_id, subject_type, subject_id, channel_id, normalized_state,
                 reason_code, provider, raw_reason, evidence_json, tool_version,
                 observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, subject_type, subject_id, channel_id, state, reason_code,
                str(result.get("provider") or "yt-dlp")[:120], raw_reason,
                _evidence_json(result.get("evidence") or {}),
                str(result.get("tool_version") or "")[:120] or None,
                observed_at,
            ),
        )
        observation_id = int(cursor.lastrowid)
        if subject_type == "channel":
            upsert_channel_identity(conn, channel_id)
            conn.execute(
                """
                UPDATE channel_identities
                SET current_state = ?, current_reason_code = ?, current_state_at = ?,
                    last_accessible_at = CASE WHEN ? = 'accessible' THEN ? ELSE last_accessible_at END,
                    revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
                """,
                (state, reason_code, observed_at, state, observed_at, channel_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO video_availability
                    (channel_id, video_id, current_state, current_reason_code,
                     current_state_at, last_accessible_at, last_observation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, video_id) DO UPDATE SET
                    current_state = excluded.current_state,
                    current_reason_code = excluded.current_reason_code,
                    current_state_at = excluded.current_state_at,
                    last_accessible_at = CASE
                        WHEN excluded.current_state = 'available' THEN excluded.current_state_at
                        ELSE video_availability.last_accessible_at
                    END,
                    last_observation_id = excluded.last_observation_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    channel_id, subject_id, state, reason_code, observed_at,
                    observed_at if state == "available" else None, observation_id,
                ),
            )
        conn.commit()
    return observation_id


def _legacy_channel_reclassification(
    row: sqlite3.Row,
) -> tuple[str, str, str] | None:
    """Return a versioned, auditable correction for one historical row."""
    original_state = str(row["normalized_state"] or "")
    original_reason = str(row["reason_code"] or "")
    provider = str(row["provider"] or "").casefold()
    if provider not in {"yt-dlp", "yt-dlp+youtube-data-api"}:
        return None

    raw_evidence = str(row["evidence_json"] or "")
    try:
        evidence = json.loads(raw_evidence or "{}")
    except (TypeError, ValueError):
        evidence = None
    if not isinstance(evidence, dict):
        evidence = None
    classifier_version = evidence.get("classifier_version") if evidence else None
    previous_classifier_version = (
        classifier_version
        if isinstance(classifier_version, str) and len(classifier_version) <= 120
        else "unknown"
    )

    # Unknown v2 results already passed the current classifier. Historical
    # explicit rows are always revalidated because v1 used a loose text search
    # that did not bind evidence to the exact requested UCID.
    if original_state != "terminated_explicit" and classifier_version != "availability-v1":
        return None

    classified = _classify_failure(
        "channel",
        str(row["raw_reason"] or ""),
        requested_subject_id=row["subject_id"],
    )
    state = str(classified.get("state") or "unavailable_unknown")
    reason_code = str(
        classified.get("reason_code") or "channel_unavailable_unknown"
    )

    if state == "terminated_explicit" and provider == "yt-dlp+youtube-data-api":
        # Missing API structure cannot corroborate or contradict the provider;
        # fail closed instead of allowing an old explicit row to unlock action.
        if evidence is None or "api_resource_exists" not in evidence:
            state, reason_code = (
                "unavailable_unknown",
                "channel_unavailable_unknown",
            )
        else:
            api = evidence.get("youtube_api")
            api_reports_accessible = evidence.get("api_resource_exists") is True or (
                isinstance(api, dict) and api.get("state") == "accessible"
            )
            if api_reports_accessible:
                state, reason_code = (
                    "conflicting_evidence",
                    "api_reports_accessible",
                )

    if (
        classifier_version == _CLASSIFIER_VERSION
        and original_state == state
        and original_reason == reason_code
    ):
        return None

    audit_entry = {
        "reclassified_at": _now(),
        "from": {
            "classifier_version": previous_classifier_version,
            "state": original_state,
            "reason_code": original_reason[:240],
        },
        "to": {
            "classifier_version": _CLASSIFIER_VERSION,
            "state": state,
            "reason_code": reason_code,
        },
        "signal": (
            "exact_channel_provider_message"
            if state in {"terminated_explicit", "conflicting_evidence"}
            else "provider_message_not_explicit"
        ),
    }
    raw_evidence_bytes = raw_evidence.encode("utf-8")
    prior_evidence = {
        "sha256": hashlib.sha256(raw_evidence_bytes).hexdigest(),
        "size_bytes": len(raw_evidence_bytes),
        "preserved_inline": len(raw_evidence_bytes) <= 2_048,
    }
    if prior_evidence["preserved_inline"]:
        prior_evidence["raw_text"] = raw_evidence

    prior_history: list[object] = []
    if evidence is not None and isinstance(
        evidence.get("classification_history"), list
    ):
        prior_history = evidence["classification_history"]
    history = prior_history[-4:] + [audit_entry]

    # Start by preserving a valid evidence object in full. Malformed legacy
    # evidence gets a new parseable envelope so even a state-preserving repair
    # is durably marked as having passed the v2 classifier.
    if evidence is not None:
        evidence["classification_history"] = history
        evidence["classifier_version"] = _CLASSIFIER_VERSION
        candidate_json = _json(evidence)
    else:
        candidate_json = _json({
            "classifier_version": _CLASSIFIER_VERSION,
            "classification_history": [audit_entry],
            "prior_evidence": prior_evidence,
        })

    if len(candidate_json) > _MAX_EVIDENCE_JSON:
        youtube_api = evidence.get("youtube_api") if evidence is not None else None
        yt_dlp = evidence.get("yt_dlp") if evidence is not None else None
        compact_evidence: dict[str, object] = {
            "classifier_version": _CLASSIFIER_VERSION,
            # Do not copy inherited history entries into a fallback envelope:
            # any one legacy entry may itself consume the storage bound.
            "classification_history": [audit_entry],
            "prior_evidence": prior_evidence,
            "api_resource_exists": (
                evidence.get("api_resource_exists")
                if evidence is not None
                and isinstance(evidence.get("api_resource_exists"), bool)
                else None
            ),
            "youtube_api": (
                {
                    key: youtube_api.get(key)
                    for key in (
                        "state",
                        "reason_code",
                        "status_code",
                        "raw_reason",
                    )
                    if key in youtube_api
                }
                if isinstance(youtube_api, dict)
                else None
            ),
            "yt_dlp": (
                {
                    key: yt_dlp.get(key)
                    for key in (
                        "returncode",
                        "reported_id",
                        "extractor",
                        "availability",
                        "raw_reason",
                    )
                    if key in yt_dlp
                }
                if isinstance(yt_dlp, dict)
                else {}
            ),
        }
        if prior_history:
            encoded_history = _json(prior_history).encode("utf-8")
            compact_evidence["prior_classification_history"] = {
                "entry_count": len(prior_history),
                "sha256": hashlib.sha256(encoded_history).hexdigest(),
                "size_bytes": len(encoded_history),
            }
        candidate_json = _json(compact_evidence)

    if len(candidate_json) > _MAX_EVIDENCE_JSON:
        # Fixed-shape last resort: all variable legacy content is represented
        # only by hashes and sizes, guaranteeing a bounded v2 audit record.
        candidate_json = _json({
            "classifier_version": _CLASSIFIER_VERSION,
            "classification_history": [audit_entry],
            "prior_evidence": {
                "sha256": prior_evidence["sha256"],
                "size_bytes": prior_evidence["size_bytes"],
                "preserved_inline": False,
            },
            "api_resource_exists": (
                evidence.get("api_resource_exists")
                if evidence is not None
                and isinstance(evidence.get("api_resource_exists"), bool)
                else None
            ),
        })

    return state, reason_code, candidate_json


def reclassify_legacy_channel_observations() -> dict[str, int]:
    """Repair availability-v1 channel-removal rows once, transactionally.

    Derived state and the versioned classification audit are updated from the
    preserved provider output. The aggregate is rebuilt from the newest exact-
    channel observation, and one registry revision bump invalidates any
    pre-repair dry-run token.
    """
    reclassified = 0
    conflicting = 0
    skipped_ambiguous_api = 0
    affected_channels: set[str] = set()
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT observation_id, subject_id, channel_id, normalized_state,
                   reason_code, provider, raw_reason, evidence_json
            FROM availability_observations
            WHERE subject_type = 'channel'
              AND subject_id = channel_id
              AND normalized_state IN (
                  'unavailable_unknown', 'terminated_explicit'
              )
              AND provider IN ('yt-dlp', 'yt-dlp+youtube-data-api')
            ORDER BY observation_id
            """
        ).fetchall()
        for row in rows:
            replacement = _legacy_channel_reclassification(row)
            if replacement is None:
                continue
            state, reason_code, evidence_json = replacement
            cursor = conn.execute(
                """
                UPDATE availability_observations
                SET normalized_state = ?, reason_code = ?, evidence_json = ?
                WHERE observation_id = ?
                  AND normalized_state = ?
                  AND reason_code IS ?
                  AND (
                      normalized_state != ? OR reason_code IS NOT ?
                      OR COALESCE(evidence_json, '') != ?
                  )
                """,
                (
                    state,
                    reason_code,
                    evidence_json,
                    row["observation_id"],
                    row["normalized_state"],
                    row["reason_code"],
                    state,
                    reason_code,
                    evidence_json,
                ),
            )
            if cursor.rowcount != 1:
                continue
            reclassified += 1
            conflicting += state == "conflicting_evidence"
            affected_channels.add(row["channel_id"])

        for channel_id in sorted(affected_channels):
            candidates = conn.execute(
                """
                SELECT observation_id, normalized_state, reason_code, observed_at
                FROM availability_observations
                WHERE subject_type = 'channel'
                  AND subject_id = ? AND channel_id = ?
                """,
                (channel_id, channel_id),
            ).fetchall()
            if not candidates:
                continue
            def aggregate_order(item: sqlite3.Row) -> tuple[datetime, int]:
                try:
                    observed = _parse_time(str(item["observed_at"] or ""))
                except (TypeError, ValueError):
                    observed = datetime.min.replace(tzinfo=timezone.utc)
                return observed, int(item["observation_id"])

            latest = max(candidates, key=aggregate_order)
            conn.execute(
                """
                UPDATE channel_identities
                SET current_state = ?, current_reason_code = ?,
                    current_state_at = ?, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ?
                """,
                (
                    latest["normalized_state"],
                    latest["reason_code"],
                    latest["observed_at"],
                    channel_id,
                ),
            )
        conn.commit()

    if reclassified:
        log(
            "🧰 [AVAILABILITY] Reclassified "
            f"{reclassified} legacy channel observations across "
            f"{len(affected_channels)} channels ({conflicting} conflicting)"
        )
    if skipped_ambiguous_api:
        log(
            "⚠️ [AVAILABILITY] Left "
            f"{skipped_ambiguous_api} legacy channel observations unknown "
            "because API corroboration evidence was incomplete"
        )
    return {
        "reclassified_observations": reclassified,
        "affected_channels": len(affected_channels),
        "conflicting_observations": conflicting,
        "skipped_ambiguous_api": skipped_ambiguous_api,
    }


def _termination_eligibility_from_rows(
    current_state: str | None,
    current_reason_code: str | None,
    rows: list[sqlite3.Row],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    def value(row: sqlite3.Row, key: str) -> Any:
        try:
            return row[key]
        except (IndexError, KeyError):
            return None

    def diagnostic(row: sqlite3.Row, **extra: Any) -> dict[str, Any]:
        return {
            "observation_id": value(row, "observation_id"),
            "observed_at": value(row, "observed_at"),
            "reason_code": value(row, "reason_code"),
            **extra,
        }

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    valid_records: list[dict[str, Any]] = []
    invalid_observations: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        raw_time = value(row, "observed_at")
        try:
            observed_time = _parse_time(str(raw_time or ""))
        except (TypeError, ValueError):
            invalid_observations.append(
                diagnostic(
                    row,
                    state=value(row, "normalized_state"),
                    issue="malformed_timestamp",
                )
            )
            continue
        if observed_time > now:
            invalid_observations.append(
                diagnostic(
                    row,
                    state=value(row, "normalized_state"),
                    issue="future_timestamp",
                )
            )
            continue
        observation_id = value(row, "observation_id")
        try:
            stable_order = int(observation_id)
        except (TypeError, ValueError):
            stable_order = index
        valid_records.append({
            "row": row,
            "time": observed_time,
            "order": stable_order,
            "index": index,
        })
    # SQLite text ordering is not chronological when ISO timestamps use mixed
    # offsets. Eligibility always operates on normalized UTC instants.
    valid_records.sort(
        key=lambda record: (
            record["time"],
            record["order"],
            record["index"],
        )
    )
    ordered_rows = [record["row"] for record in valid_records]
    times = {id(record["row"]): record["time"] for record in valid_records}
    explicit_rows = [
        row
        for row in ordered_rows
        if value(row, "normalized_state") == "terminated_explicit"
    ]
    reason = (
        (
            current_reason_code
            if current_state == "terminated_explicit"
            else value(explicit_rows[-1], "reason_code")
        )
        if explicit_rows
        else None
    )
    base = {
        "eligible": False,
        "status": "no_matching_evidence",
        "required_observations": 2,
        "minimum_separation_hours": 24,
        "matching_observations": 0,
        "historical_matching_observations": 0,
        "qualifying_observations": 0,
        "qualifying_observations_needed": 2,
        "remaining_observations": 2,
        "threshold_progress": 0,
        "matching_reason_code": reason,
        "first_observed_at": None,
        "latest_observed_at": None,
        "next_qualifying_observation_at": None,
        "next_qualifying_check_at": None,
        "qualified_at": None,
        "counting_observations": [],
        "last_blocking_observation": None,
        "invalid_observations": invalid_observations[
            -_MAX_ELIGIBILITY_DIAGNOSTIC_OBSERVATIONS:
        ],
        "invalid_observation_count": len(invalid_observations),
        "api_corroboration_required": False,
        "reason_code": reason,
    }
    invalid_blockers = [
        item
        for item in invalid_observations
        if item.get("state") in {"accessible", "conflicting_evidence"}
    ]
    if invalid_blockers:
        # An accessible/conflicting row with an unusable timestamp cannot be
        # placed safely before or after the explicit sequence. It must fail
        # closed instead of being discarded and accidentally allowing action.
        base["status"] = "invalid_timestamp_evidence"
        base["last_blocking_observation"] = invalid_blockers[-1]
        return base
    matching = [
        row
        for row in ordered_rows
        if value(row, "normalized_state") == "terminated_explicit"
        and value(row, "reason_code") == reason
    ]
    base["historical_matching_observations"] = len(matching)

    blocking_indexes = [
        index
        for index, row in enumerate(ordered_rows)
        if value(row, "normalized_state")
        in {"accessible", "conflicting_evidence"}
    ]
    last_blocking_index = blocking_indexes[-1] if blocking_indexes else -1
    if last_blocking_index >= 0:
        blocking_row = ordered_rows[last_blocking_index]
        base["last_blocking_observation"] = {
            **diagnostic(blocking_row),
            "state": value(blocking_row, "normalized_state"),
        }

    counting = [
        row
        for index, row in enumerate(ordered_rows)
        if index > last_blocking_index
        and value(row, "normalized_state") == "terminated_explicit"
        and value(row, "reason_code") == reason
    ]
    base["matching_observations"] = len(counting)
    baseline = counting[0] if counting else None
    qualifying_second = None
    if baseline is not None:
        baseline_time = times[id(baseline)]
        qualifying_second = next(
            (
                row
                for row in counting[1:]
                if times[id(row)] - baseline_time
                >= _TERMINATION_CONFIRMATION_INTERVAL
            ),
            None,
        )
    if counting:
        base["first_observed_at"] = value(counting[0], "observed_at")
        base["latest_observed_at"] = value(counting[-1], "observed_at")
        base["counting_observations"] = [diagnostic(baseline)]
        if qualifying_second is None:
            base["threshold_progress"] = 1
            base["qualifying_observations"] = 1
            base["qualifying_observations_needed"] = 1
            base["remaining_observations"] = 1
            next_qualifying = (
                times[id(baseline)] + _TERMINATION_CONFIRMATION_INTERVAL
            )
            next_qualifying_text = next_qualifying.isoformat()
            base["next_qualifying_observation_at"] = next_qualifying_text
            base["next_qualifying_check_at"] = next_qualifying_text
        else:
            base["threshold_progress"] = 2
            base["qualifying_observations"] = 2
            base["qualifying_observations_needed"] = 0
            base["remaining_observations"] = 0
            base["qualified_at"] = value(qualifying_second, "observed_at")
            base["counting_observations"] = [
                diagnostic(baseline),
                diagnostic(qualifying_second),
            ]

    if current_state != "terminated_explicit":
        last_matching_index = max(
            (
                index
                for index, row in enumerate(ordered_rows)
                if value(row, "normalized_state") == "terminated_explicit"
                and value(row, "reason_code") == reason
            ),
            default=-1,
        )
        if last_matching_index >= 0 and last_blocking_index > last_matching_index:
            base["status"] = "evidence_reset"
            base["threshold_progress"] = 0
            base["qualifying_observations"] = 0
            base["qualifying_observations_needed"] = 2
            base["remaining_observations"] = 2
            base["counting_observations"] = []
            base["first_observed_at"] = None
            base["latest_observed_at"] = None
            base["next_qualifying_observation_at"] = None
            base["next_qualifying_check_at"] = None
        elif matching:
            base["status"] = "latest_not_explicit"
        elif invalid_observations:
            base["status"] = "invalid_timestamp_evidence"
        return base

    if not counting:
        if invalid_observations:
            base["status"] = "invalid_timestamp_evidence"
        return base

    if qualifying_second is not None:
        base["eligible"] = True
        base["status"] = "eligible"
        return base
    next_qualifying = times[id(baseline)] + _TERMINATION_CONFIRMATION_INTERVAL
    base["status"] = (
        "ready_for_followup" if now >= next_qualifying else "waiting_for_interval"
    )
    return base


def termination_eligibility(channel_id: str) -> dict[str, Any]:
    """Return repeated-explicit-evidence eligibility; never confirms a designation."""
    channel_id = validate_channel_id(channel_id)
    with get_db() as conn:
        identity = conn.execute(
            "SELECT current_state, current_reason_code FROM channel_identities WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT observation_id, normalized_state, reason_code, observed_at
            FROM availability_observations
            WHERE subject_type = 'channel' AND subject_id = ?
            ORDER BY observed_at, observation_id
            """,
            (channel_id,),
        ).fetchall()
    return _termination_eligibility_from_rows(
        identity["current_state"] if identity else None,
        identity["current_reason_code"] if identity else None,
        rows,
    )


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"], "kind": row["kind"], "channel_id": row["channel_id"],
        "status": row["status"], "current": int(row["current"]), "total": int(row["total"]),
        "phase": row["phase"],
        "success_count": int(row["success_count"]), "fail_count": int(row["fail_count"]),
        "cancel_requested": bool(row["cancel_requested"]), "error": row["error"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def get_job(job_id: str) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM availability_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise AvailabilityError("Availability job not found", 404)
    return _job_row(row)


def _job_cancel_requested(job_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM availability_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


def _persist_worker_failure(job_id: str, exc: Exception) -> None:
    message = (str(exc) or exc.__class__.__name__)[:2000]
    try:
        with get_db() as conn:
            conn.execute(
                """
                UPDATE availability_jobs
                SET status = CASE
                        WHEN status = 'cancelling' OR cancel_requested = 1
                        THEN 'cancelled' ELSE 'failed' END,
                    phase = CASE
                        WHEN status = 'cancelling' OR cancel_requested = 1
                        THEN 'cancelled' ELSE 'failed' END,
                    completed_at = ?,
                    error = CASE
                        WHEN status = 'cancelling' OR cancel_requested = 1
                        THEN error ELSE ? END
                WHERE job_id = ? AND status IN ('queued', 'running', 'cancelling')
                """,
                (_now(), message, job_id),
            )
            conn.commit()
    except Exception as persist_error:  # noqa: BLE001
        log(f"Availability job {job_id} failed and could not persist its error: {persist_error}")


def _launch(job_id: str) -> None:
    def runner() -> None:
        try:
            run_job(job_id)
        except Exception as exc:  # noqa: BLE001
            _persist_worker_failure(job_id, exc)
        finally:
            with _runtime_lock:
                _threads.pop(job_id, None)

    thread = threading.Thread(target=runner, daemon=True, name=f"availability-{job_id[:8]}")
    with _runtime_lock:
        _threads[job_id] = thread
    try:
        thread.start()
    except Exception as exc:
        with _runtime_lock:
            _threads.pop(job_id, None)
        _persist_worker_failure(job_id, exc)
        raise AvailabilityError(f"Could not start availability worker: {exc}", 503) from exc


def _start_job(
    kind: str,
    channel_id: str,
    video_ids: list[str],
    *,
    actor: str | None,
    background: bool,
    job_payload: dict[str, Any] | None = None,
    initial_total: int | None = None,
) -> dict[str, Any]:
    channel_id = validate_channel_id(channel_id)
    subjects = [channel_id] if kind == "channel" else video_ids
    job_id = secrets.token_hex(12)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active_count = conn.execute(
            "SELECT COUNT(*) AS count FROM availability_jobs "
            "WHERE status IN ('queued', 'running', 'cancelling')"
        ).fetchone()["count"]
        if int(active_count) >= AVAILABILITY_MAX_CONCURRENT_JOBS:
            raise AvailabilityError(
                "The availability worker queue is full; wait for the active job",
                429,
            )
        identity = conn.execute(
            "SELECT 1 FROM channel_identities WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if identity is None:
            if kind == "channel":
                upsert_channel_identity(conn, channel_id)
            else:
                raise AvailabilityError("Channel identity not found", 404)
        payload = job_payload or {"subject_ids": subjects}
        total = len(subjects) if initial_total is None else initial_total
        try:
            conn.execute(
                """
                INSERT INTO availability_jobs
                    (job_id, kind, channel_id, status, total, payload_json, created_by)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, kind, channel_id, total, _json(payload), actor),
            )
        except sqlite3.IntegrityError as exc:
            raise AvailabilityError("This channel already has an active availability job", 409) from exc
        conn.commit()
        row = conn.execute("SELECT * FROM availability_jobs WHERE job_id = ?", (job_id,)).fetchone()
        job = _job_row(row)
    if background:
        _launch(job_id)
    else:
        try:
            run_job(job_id)
        except Exception as exc:  # mirror the background worker contract
            _persist_worker_failure(job_id, exc)
        job = get_job(job_id)
    return job


def start_channel_check(channel_id: str, *, actor: str | None = None, background: bool = True) -> dict[str, Any]:
    return _start_job("channel", channel_id, [], actor=actor, background=background)


def start_video_check(
    channel_id: str,
    video_ids: list[str] | None = None,
    *,
    actor: str | None = None,
    background: bool = True,
) -> dict[str, Any]:
    channel_id = validate_channel_id(channel_id)
    requested_ids: list[str] | None = None
    if video_ids is not None:
        if not isinstance(video_ids, list) or not video_ids:
            raise AvailabilityError("video_ids must be a non-empty array when supplied")
        if len(video_ids) > _MAX_VIDEO_JOB_SUBJECTS:
            raise AvailabilityError(
                f"video_ids must contain unique exact IDs (maximum {_MAX_VIDEO_JOB_SUBJECTS})"
            )
        if any(not isinstance(item, str) or not is_valid_youtube_id(item) for item in video_ids):
            raise AvailabilityError("Every video ID must be an exact 11-character YouTube ID")
        if len(set(video_ids)) != len(video_ids):
            raise AvailabilityError(
                f"video_ids must contain unique exact IDs (maximum {_MAX_VIDEO_JOB_SUBJECTS})"
            )
        requested_ids = list(video_ids)
    with get_db() as conn:
        upsert_channel_identity(conn, channel_id, provenance="source")
        conn.commit()
    # Inventory discovery belongs to the durable job. The request returns a job
    # ID immediately even when the selected channel lives on a slow NAS mount.
    return _start_job(
        "videos",
        channel_id,
        [],
        actor=actor,
        background=background,
        job_payload={
            "discover_inventory": True,
            "requested_ids": requested_ids,
        },
        initial_total=len(requested_ids or []),
    )


def run_job(job_id: str) -> None:
    with _job_guard:
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM availability_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise AvailabilityError("Availability job not found", 404)
            if row["status"] == "cancelling" or row["cancel_requested"]:
                conn.execute(
                    """
                    UPDATE availability_jobs
                    SET status = 'cancelled', phase = 'cancelled', completed_at = ?
                    WHERE job_id = ?
                    """,
                    (_now(), job_id),
                )
                conn.commit()
                return
            if row["status"] != "queued":
                conn.rollback()
                return
            payload = _decode_json(row["payload_json"], {})
            subject_ids = payload.get("subject_ids") if isinstance(payload, dict) else []
            if not isinstance(subject_ids, list):
                subject_ids = []
            kind = row["kind"]
            channel_id = row["channel_id"]
            discover_inventory = bool(
                kind == "videos"
                and isinstance(payload, dict)
                and payload.get("discover_inventory")
            )
            conn.execute(
                """
                UPDATE availability_jobs
                SET status = 'running', phase = ?, started_at = ?, error = NULL
                WHERE job_id = ?
                """,
                ("inventory" if discover_inventory else "probing", _now(), job_id),
            )
            conn.commit()

        if discover_inventory:
            inventory = _local_inventory(channel_id, force_source_scan=True)
            if _job_cancel_requested(job_id):
                with get_db() as conn:
                    conn.execute(
                        """
                        UPDATE availability_jobs
                        SET status = 'cancelled', phase = 'cancelled', completed_at = ?
                        WHERE job_id = ? AND status IN ('running', 'cancelling')
                        """,
                        (_now(), job_id),
                    )
                    conn.commit()
                return
            requested_ids = payload.get("requested_ids")
            if requested_ids is None:
                subject_ids = sorted(inventory)
            elif isinstance(requested_ids, list):
                missing = sorted(set(requested_ids) - set(inventory))
                if missing:
                    raise AvailabilityError(
                        "Every requested video ID must belong to the local channel inventory",
                        409,
                    )
                subject_ids = list(requested_ids)
            else:
                raise AvailabilityError("Stored video job selection is invalid", 409)
            if len(subject_ids) > _MAX_VIDEO_JOB_SUBJECTS:
                raise AvailabilityError(
                    f"A single availability job is limited to {_MAX_VIDEO_JOB_SUBJECTS} local video IDs; use the filtered-page batch action",
                    409,
                )
            if not subject_ids:
                raise AvailabilityError(
                    "No local exact-ID videos were found for this channel", 409
                )
            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """
                    SELECT status, cancel_requested
                    FROM availability_jobs WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if current is None:
                    raise AvailabilityError("Availability job not found", 404)
                if current["status"] == "cancelling" or current["cancel_requested"]:
                    conn.execute(
                        """
                        UPDATE availability_jobs
                        SET status = 'cancelled', phase = 'cancelled', completed_at = ?
                        WHERE job_id = ?
                        """,
                        (_now(), job_id),
                    )
                    conn.commit()
                    return
                conn.execute(
                    """
                    UPDATE availability_jobs
                    SET total = ?, phase = 'probing', payload_json = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (
                        len(subject_ids),
                        _json({"subject_ids": subject_ids}),
                        job_id,
                    ),
                )
                conn.commit()

        successes = 0
        failures = 0
        paused = False
        cancelled = False
        include_api = bool(YOUTUBE_API_KEY)
        for position, subject_id in enumerate(subject_ids, start=1):
            if _job_cancel_requested(job_id):
                cancelled = True
                break
            subject_type = "channel" if kind == "channel" else "video"
            result = probe(
                subject_type,
                subject_id,
                expected_channel_id=channel_id,
                include_api=include_api,
            )
            if result.get("disable_api"):
                include_api = False
            record_observation(job_id, subject_type, subject_id, channel_id, result)
            if result["state"] == "transient_failure":
                failures += 1
            else:
                successes += 1
            paused = bool(result.get("pause"))
            with get_db() as conn:
                conn.execute(
                    "UPDATE availability_jobs SET current = ?, success_count = ?, fail_count = ? WHERE job_id = ?",
                    (position, successes, failures, job_id),
                )
                conn.commit()
            if paused:
                break
            if position < len(subject_ids) and AVAILABILITY_PROBE_DELAY_SECONDS:
                time.sleep(AVAILABILITY_PROBE_DELAY_SECONDS)

        # Cancellation can arrive while the final subprocess or API request is
        # in flight. Do not overwrite that durable request with "completed".
        if not cancelled and _job_cancel_requested(job_id):
            cancelled = True
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            final_row = conn.execute(
                "SELECT status, cancel_requested FROM availability_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            cancelled = bool(
                cancelled
                or (final_row and final_row["cancel_requested"])
                or (final_row and final_row["status"] == "cancelling")
            )
            final_status = (
                "cancelled" if cancelled else "paused" if paused else "completed"
            )
            error = (
                "Availability checks paused after a rate limit or bot challenge"
                if paused and not cancelled
                else f"{failures} probe(s) were inconclusive"
                if failures and not cancelled
                else None
            )
            conn.execute(
                """
                UPDATE availability_jobs
                SET status = ?, phase = ?, success_count = ?, fail_count = ?,
                    completed_at = ?, error = ?
                WHERE job_id = ? AND status IN ('running', 'cancelling')
                """,
                (
                    final_status,
                    final_status,
                    successes,
                    failures,
                    _now(),
                    error,
                    job_id,
                ),
            )
            conn.commit()


def cancel_job(job_id: str) -> dict[str, Any]:
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM availability_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise AvailabilityError("Availability job not found", 404)
        if row["status"] in _TERMINAL_STATES:
            conn.rollback()
            return _job_row(row)
        now = _now()
        conn.execute(
            """
            UPDATE availability_jobs
            SET cancel_requested = 1,
                status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END,
                phase = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END,
                completed_at = CASE WHEN status = 'queued' THEN ? ELSE completed_at END
            WHERE job_id = ?
            """,
            (now, job_id),
        )
        conn.commit()
    return get_job(job_id)
