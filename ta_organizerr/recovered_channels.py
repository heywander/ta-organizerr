"""Durable, read-only investigation and guarded organization of lost channels.

The source archive is immutable in this workflow.  Inspection delegates to the
same descriptor-safe Media Salvage diagnosis used by the interactive tool.
Organization publishes only a new symlink beneath ``TARGET_DIR`` and records
the result after the inspected source bytes have been re-verified.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as db_module
from . import salvage
from .config import HIDDEN_DIR, HOST_SOURCE_BASE, SOURCE_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .library_lock import LIBRARY_MUTATION_LOCK
from .media_reservations import (
    MediaReservationConflict,
    media_mutation_guard,
    release_media,
    reserve_media,
)
from .utils import (
    extract_id_from_filename,
    is_valid_youtube_id,
    is_video,
    path_in_allowed_tree,
    path_within,
    resolve_media_source,
    sanitize,
)


_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_NAME_LENGTH = 160
_MAX_BATCH = 500
_MAX_PAGE_SIZE = 500
_TERMINAL_JOB_STATES = {
    "completed", "partial", "failed", "cancelled", "interrupted"
}
_ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}

_runtime_lock = threading.RLock()
_threads: dict[str, threading.Thread] = {}
_active_salvage_jobs: dict[str, str] = {}
_orphan_jobs_recovered = False


class RecoveredChannelError(RuntimeError):
    """An operator-facing workflow error with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class RecoveredChannelMutationError(OSError):
    """Raised when another workflow attempts to mutate immutable case media."""


def _ownership_rows(query: str) -> list[sqlite3.Row]:
    """Read recovery ownership without breaking pre-bootstrap utility callers.

    A few low-level recovery helpers are intentionally usable before the app
    has initialized its database.  An absent database, or an existing empty
    SQLite file with neither ownership table, therefore means that no case has
    claimed media yet.  All other database failures are allowed to propagate:
    treating a locked, unreadable, or corrupt ownership database as empty
    would make a mutating caller fail open.
    """
    try:
        os.lstat(db_module.DB_PATH)
    except FileNotFoundError:
        return []
    except OSError:
        # Permission and I/O failures are not proof that ownership is absent.
        raise

    try:
        with get_db() as conn:
            return list(conn.execute(query).fetchall())
    except sqlite3.OperationalError as exc:
        message = str(exc).strip().lower()
        missing_tables = {
            "no such table: recovered_channel_items",
            "no such table: videos",
        }
        if message in missing_tables:
            return []
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _decode_json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _event(
    conn: sqlite3.Connection,
    case_id: str,
    event: str,
    *,
    actor: str | None = None,
    item_id: str | None = None,
    details: object = None,
) -> None:
    conn.execute(
        """
        INSERT INTO recovered_channel_events
            (case_id, item_id, actor, event, details_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (case_id, item_id, actor, event, _json(details) if details is not None else None),
    )


def _validated_channel_id(value: object) -> str:
    if not isinstance(value, str) or _CHANNEL_ID_RE.fullmatch(value) is None:
        raise RecoveredChannelError(
            "source_channel_id must be an exact 24-character UC channel ID"
        )
    return value


def _validated_target_name(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise RecoveredChannelError("target_channel_name must be a string")
    name = value.strip()
    if not name or len(name) > _MAX_NAME_LENGTH:
        raise RecoveredChannelError(
            f"target_channel_name must contain 1 to {_MAX_NAME_LENGTH} characters"
        )
    if name.casefold().endswith("[recovered]"):
        name = name[: -len("[Recovered]")].rstrip()
    if not name:
        raise RecoveredChannelError("A channel name is required before [Recovered]")
    # This workflow promises the displayed name is the actual folder name.
    # Reject lossy input rather than silently changing the operator's choice.
    reserved_windows_names = {
        "con", "prn", "aux", "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
    if (
        sanitize(name) != name
        or "\\" in name
        or name in {".", ".."}
        or name[-1] in {".", " "}
        or name.split(".", 1)[0].casefold() in reserved_windows_names
        or unicodedata.normalize("NFC", name) != name
    ):
        raise RecoveredChannelError(
            "target_channel_name contains characters that are unsafe in a folder name"
        )
    component = f"{name} [Recovered]"
    if sanitize(component) != component:
        raise RecoveredChannelError("The recovered channel folder name is unsafe")
    return name, component


def _channel_root(channel_id: str) -> Path:
    channel_id = _validated_channel_id(channel_id)
    root = SOURCE_DIR / channel_id
    try:
        entry = os.lstat(root)
        canonical = root.resolve(strict=True)
    except OSError as exc:
        raise RecoveredChannelError(
            f"Source channel folder is not available: {exc}", 404
        ) from None
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        raise RecoveredChannelError(
            "Source channel entry must be a real directory, not a symlink", 400
        )
    if not path_within(canonical, (SOURCE_DIR,)):
        raise RecoveredChannelError("Source channel folder escaped SOURCE_DIR", 400)
    return root


def _discover_channel(channel_id: str) -> list[dict[str, Any]]:
    root = _channel_root(channel_id)
    # Recovery cases own only files that have no existing library owner. This
    # mirrors the Unindexed entry point and prevents an entire mixed raw channel
    # folder from accidentally claiming normal TubeArchivist-managed media.
    with get_db() as conn:
        owned_video_ids = {
            row["video_id"]
            for row in conn.execute("SELECT video_id FROM videos")
            if isinstance(row["video_id"], str)
        }
    found: list[dict[str, Any]] = []
    try:
        for current, dirs, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            safe_dirs: list[str] = []
            for name in dirs:
                candidate = current_path / name
                try:
                    candidate_stat = os.lstat(candidate)
                except OSError:
                    continue
                if stat.S_ISDIR(candidate_stat.st_mode) and not stat.S_ISLNK(
                    candidate_stat.st_mode
                ):
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in names:
                path = current_path / name
                if not is_video(path):
                    continue
                try:
                    file_stat = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
                    continue
                if file_stat.st_size <= 0 or not path_within(path, (root,)):
                    continue
                video_id = extract_id_from_filename(path.name)
                if not video_id or not is_valid_youtube_id(video_id):
                    continue
                if video_id in owned_video_ids:
                    continue
                found.append({
                    "video_id": video_id,
                    "source_path": str(path),
                    # The configured media allow-list is case-insensitive, but
                    # publication preserves the archive entry's exact suffix.
                    "suffix": path.suffix,
                    "size_bytes": file_stat.st_size,
                    "device": file_stat.st_dev,
                    "inode": file_stat.st_ino,
                    "mtime_ns": file_stat.st_mtime_ns,
                    "ctime_ns": file_stat.st_ctime_ns,
                })
    except OSError as exc:
        raise RecoveredChannelError(f"Could not scan source channel: {exc}", 503) from exc
    return sorted(found, key=lambda item: (item["video_id"], item["source_path"]))


def _case_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "source_channel_id": row["source_channel_id"],
        "target_channel_name": row["target_channel_name"],
        "target_component": row["target_component"],
        "target_folder_path": str(TARGET_DIR / row["target_component"]),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _item_row(row: sqlite3.Row) -> dict[str, Any]:
    diagnosis = _decode_json(row["diagnosis_json"], None)
    findings: list[dict[str, Any]] = []
    if isinstance(diagnosis, dict) and isinstance(diagnosis.get("workflow_findings"), list):
        findings = diagnosis["workflow_findings"]
    if "source_channel_id" in row.keys() and "target_component" in row.keys():
        source_channel_id = row["source_channel_id"]
        target_component = row["target_component"]
    else:
        source_channel_id, target_component = _item_case_context(row["case_id"])
    link_state = _item_link_state(
        row,
        source_channel_id=source_channel_id,
        target_component=target_component,
    )
    return {
        "item_id": row["item_id"],
        "case_id": row["case_id"],
        "source_channel_id": source_channel_id,
        "video_id": row["video_id"],
        "source_path": row["source_path"],
        "filename": Path(row["source_path"]).name,
        "suffix": row["suffix"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "inspection_status": row["inspection_status"],
        "health_tier": row["health_tier"],
        "diagnosis": diagnosis,
        "findings": findings,
        "expected_duration_seconds": row["expected_duration_seconds"],
        "decision": row["decision"],
        "organized_path": row["organized_path"],
        "organized_at": row["organized_at"],
        "error": row["error"],
        "manual_title": row["manual_title"],
        "manual_published": row["manual_published"],
        "metadata_source_url": row["metadata_source_url"],
        "metadata_notes": row["metadata_notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "preview_url": "/api/recovery/preview?filepath=" + _quote_path(row["source_path"]),
        "preview_audio_url": "/api/recovery/preview-audio?filepath=" + _quote_path(row["source_path"]),
        **link_state,
    }


def _item_case_context(case_id: str) -> tuple[str | None, str | None]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT source_channel_id, target_component "
            "FROM recovered_channel_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row["source_channel_id"], row["target_component"]


def _item_link_state(
    row: sqlite3.Row,
    *,
    source_channel_id: str | None,
    target_component: str | None,
) -> dict[str, Any]:
    source = Path(row["source_path"])
    try:
        link_target: str | None = str(
            HOST_SOURCE_BASE / source.relative_to(SOURCE_DIR)
        )
    except ValueError:
        link_target = None
    filename = row["video_id"] + row["suffix"]
    planned_path = (
        TARGET_DIR / target_component / filename
        if isinstance(target_component, str)
        else None
    )
    organized = (
        Path(row["organized_path"])
        if isinstance(row["organized_path"], str)
        else None
    )
    if isinstance(source_channel_id, str):
        source_error = _fingerprint_error(row, source_channel_id)
    else:
        source_error = "Recovered case channel context is unavailable"
    expected_path = organized or planned_path
    owner_valid = bool(
        organized is not None
        and "recovered_owner_status" in row.keys()
        and row["recovered_owner_status"] == "recovered"
        and row["recovered_owner_symlink"] == str(organized)
    )
    actual_target: str | None = None
    if organized is None:
        status_value = "unorganized"
    elif not isinstance(target_component, str):
        status_value = "unsafe"
    else:
        managed_root = next(
            (
                root
                for root in (TARGET_DIR, HIDDEN_DIR)
                if organized == root / target_component / filename
            ),
            None,
        )
        if managed_root is None:
            status_value = "unsafe"
        else:
            entry_state, parent_status = _read_managed_link_state(
                managed_root, target_component, filename
            )
            if parent_status is not None:
                status_value = parent_status
            elif entry_state is None:
                status_value = "missing"
            elif entry_state["mode"] == stat.S_IFLNK:
                actual_target = entry_state["target"]
                if actual_target != link_target:
                    status_value = "wrong_symlink"
                elif source_error is not None:
                    status_value = "source_invalid"
                else:
                    status_value = "healthy"
            elif entry_state["mode"] == stat.S_IFREG:
                status_value = "unexpected_real_file"
            else:
                status_value = "unsafe"
    if organized is not None and status_value == "healthy" and not owner_valid:
        status_value = "owner_invalid"
    owner_error = (
        None
        if owner_valid or organized is None
        else "The recovered videos row no longer owns this exact organized path"
    )
    repairable = bool(
        status_value == "missing"
        and owner_valid
        and source_error is None
        and isinstance(row["sha256"], str)
        and _SHA256_RE.fullmatch(row["sha256"]) is not None
        and link_target is not None
    )
    return {
        "link_status": status_value,
        "repairable_link": repairable,
        "expected_organized_path": str(expected_path) if expected_path else None,
        "link_target": link_target,
        "actual_link_target": actual_target,
        "source_integrity_error": source_error,
        "database_owner_valid": owner_valid,
        "database_owner_error": owner_error,
    }


def _repair_needed_count(
    conn: sqlite3.Connection, case: sqlite3.Row
) -> int:
    rows = conn.execute(
        """
        SELECT i.*, v.status AS recovered_owner_status,
               v.symlink AS recovered_owner_symlink
        FROM recovered_channel_items AS i
        LEFT JOIN videos AS v ON v.video_id = i.video_id
        WHERE i.case_id = ? AND i.organized_path IS NOT NULL
        """,
        (case["case_id"],),
    ).fetchall()
    return sum(
        1
        for row in rows
        if _item_link_state(
            row,
            source_channel_id=case["source_channel_id"],
            target_component=case["target_component"],
        )["repairable_link"]
    )


def _quote_path(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "job_id": row["job_id"],
        "case_id": row["case_id"],
        "kind": row["kind"],
        "status": row["status"],
        "current": row["current"],
        "total": row["total"],
        "success_count": row["success_count"],
        "fail_count": row["fail_count"],
        "cancel_requested": bool(row["cancel_requested"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _recover_orphaned_jobs() -> None:
    global _orphan_jobs_recovered
    with _runtime_lock:
        if _orphan_jobs_recovered:
            return
        live_ids = {job_id for job_id, thread in _threads.items() if thread.is_alive()}
        with get_db() as conn:
            rows = conn.execute(
                "SELECT job_id, case_id, kind FROM recovered_channel_jobs "
                "WHERE status IN ('queued', 'running', 'cancelling')"
            ).fetchall()
            for row in rows:
                if row["job_id"] in live_ids:
                    continue
                conn.execute(
                    """
                    UPDATE recovered_channel_jobs
                    SET status = 'interrupted', completed_at = ?,
                        error = 'Server restarted before the job completed'
                    WHERE job_id = ?
                    """,
                    (_now(), row["job_id"]),
                )
                if row["kind"] == "inspect":
                    conn.execute(
                        """
                        UPDATE recovered_channel_items
                        SET inspection_status = 'pending', decision = 'pending',
                            error = 'Inspection was interrupted; resume the case',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE case_id = ? AND inspection_status = 'inspecting'
                        """,
                        (row["case_id"],),
                    )
                _event(
                    conn,
                    row["case_id"],
                    "job_interrupted",
                    details={"job_id": row["job_id"], "kind": row["kind"]},
                )
            conn.commit()
        _orphan_jobs_recovered = True


def _require_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM recovered_channel_cases WHERE case_id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise RecoveredChannelError("Recovered channel case not found", 404)
    return row


def _target_component_collision(
    conn: sqlite3.Connection,
    component: str,
    *,
    exclude_case_id: str | None = None,
) -> str | None:
    folded = unicodedata.normalize("NFC", component).casefold()
    rows = conn.execute(
        "SELECT case_id, target_component FROM recovered_channel_cases"
    ).fetchall()
    for row in rows:
        if row["case_id"] == exclude_case_id:
            continue
        if unicodedata.normalize("NFC", row["target_component"]).casefold() == folded:
            return f"Recovered case {row['case_id']} already owns that target name"
    for row in conn.execute(
        "SELECT DISTINCT channel FROM videos WHERE channel IS NOT NULL"
    ):
        if unicodedata.normalize("NFC", row["channel"]).casefold() == folded:
            return "An existing library channel already owns that target name"
    if TARGET_DIR.is_dir():
        try:
            for entry in TARGET_DIR.iterdir():
                if unicodedata.normalize("NFC", entry.name).casefold() != folded:
                    continue
                if entry.name == component:
                    # The exact empty directory may have been prepared by the
                    # operator for this new case; create_case validates emptiness.
                    continue
                return "A case-insensitive target folder collision already exists"
        except OSError as exc:
            return f"Could not validate target folder names: {exc}"
    return None


def _require_item(
    conn: sqlite3.Connection, case_id: str, item_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM recovered_channel_items WHERE case_id = ? AND item_id = ?",
        (case_id, item_id),
    ).fetchone()
    if row is None:
        raise RecoveredChannelError("Recovered channel item not found", 404)
    return row


def _active_job(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM recovered_channel_jobs
        WHERE case_id = ? AND status IN ('queued', 'running', 'cancelling')
        ORDER BY created_at DESC LIMIT 1
        """,
        (case_id,),
    ).fetchone()


def _insert_discovered(
    conn: sqlite3.Connection, case_id: str, discovered: list[dict[str, Any]]
) -> None:
    for item in discovered:
        existing = conn.execute(
            """
            SELECT * FROM recovered_channel_items
            WHERE case_id = ? AND source_path = ?
            """,
            (case_id, item["source_path"]),
        ).fetchone()
        fingerprint = tuple(
            item[name]
            for name in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns")
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO recovered_channel_items
                    (item_id, case_id, video_id, source_path, suffix,
                     size_bytes, device, inode, mtime_ns, ctime_ns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(12),
                    case_id,
                    item["video_id"],
                    item["source_path"],
                    item["suffix"],
                    *fingerprint,
                ),
            )
            continue
        previous = tuple(
            existing[name]
            for name in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns")
        )
        if fingerprint != previous and not existing["organized_path"]:
            conn.execute(
                """
                UPDATE recovered_channel_items
                SET video_id = ?, suffix = ?, size_bytes = ?, device = ?, inode = ?,
                    mtime_ns = ?, ctime_ns = ?, sha256 = NULL,
                    inspection_status = 'pending', health_tier = NULL,
                    diagnosis_json = NULL, decision = 'pending',
                    error = 'Source changed; inspection is required again',
                    updated_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (
                    item["video_id"],
                    item["suffix"],
                    *fingerprint,
                    existing["item_id"],
                ),
            )


def _sync_case_inventory(case_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        case = _require_case(conn, case_id)
        discovered = _discover_channel(case["source_channel_id"])
        _insert_discovered(conn, case_id, discovered)
        current_paths = {item["source_path"] for item in discovered}
        existing = conn.execute(
            "SELECT item_id, source_path, organized_path FROM recovered_channel_items "
            "WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        for row in existing:
            if row["source_path"] in current_paths or row["organized_path"]:
                continue
            conn.execute(
                """
                UPDATE recovered_channel_items
                SET inspection_status = 'inconclusive', health_tier = 'inconclusive',
                    decision = 'pending', error = 'Source file is no longer present',
                    updated_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (row["item_id"],),
            )
        cursor = conn.execute(
            "UPDATE recovered_channel_cases SET updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        conn.commit()
    return discovered


def create_case(
    source_channel_id: str,
    target_channel_name: str,
    actor: str | None = None,
) -> dict[str, Any]:
    channel_id = _validated_channel_id(source_channel_id)
    name, component = _validated_target_name(target_channel_name)
    discovered = _discover_channel(channel_id)
    # Only the short revalidation/claim is serialized with in-app media
    # writers. The recursive network-directory walk above does not freeze
    # unrelated library actions.
    with LIBRARY_MUTATION_LOCK:
        return _create_case_locked(
            channel_id,
            name,
            actor,
            discovered=discovered,
            target_component=component,
        )


def _create_case_locked(
    source_channel_id: str,
    target_channel_name: str,
    actor: str | None = None,
    *,
    discovered: list[dict[str, Any]] | None = None,
    target_component: str | None = None,
) -> dict[str, Any]:
    _recover_orphaned_jobs()
    channel_id = _validated_channel_id(source_channel_id)
    name, component = _validated_target_name(target_channel_name)
    component = target_component or component
    discovered = list(discovered) if discovered is not None else _discover_channel(channel_id)
    if not discovered:
        raise RecoveredChannelError(
            "No non-empty exact-ID media files were found in this source channel", 404
        )
    try:
        with get_db() as conn:
            newly_owned = {
                row["video_id"]
                for row in conn.execute("SELECT video_id FROM videos")
            }
        for item in discovered:
            if item["video_id"] in newly_owned:
                raise RecoveredChannelError(
                    f"Video {item['video_id']} gained a library owner during discovery; rescan",
                    409,
                )
            current = os.lstat(item["source_path"])
            if (
                not stat.S_ISREG(current.st_mode)
                or (
                    current.st_size,
                    current.st_dev,
                    current.st_ino,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                != (
                    item["size_bytes"],
                    item["device"],
                    item["inode"],
                    item["mtime_ns"],
                    item["ctime_ns"],
                )
            ):
                raise RecoveredChannelError(
                    "Source inventory changed during case creation; rescan", 409
                )
        # Check the complete candidate set in one registry snapshot.  The
        # caller retains LIBRARY_MUTATION_LOCK through the durable claim, so a
        # transcode/salvage lease cannot appear after this check but before the
        # case rows are committed.
        with media_mutation_guard(
            paths=(item["source_path"] for item in discovered),
            action="claim this source for immutable recovered-channel review",
        ):
            pass
    except MediaReservationConflict as exc:
        raise RecoveredChannelError(str(exc), 409) from exc
    target_folder = TARGET_DIR / component
    if os.path.lexists(target_folder):
        try:
            folder_stat = os.lstat(target_folder)
            if stat.S_ISLNK(folder_stat.st_mode) or not stat.S_ISDIR(folder_stat.st_mode):
                raise RecoveredChannelError(
                    "The recovered target path is not a real directory", 409
                )
            if any(target_folder.iterdir()):
                raise RecoveredChannelError(
                    "The recovered target folder already exists and is not empty", 409
                )
        except OSError as exc:
            raise RecoveredChannelError(f"Could not validate target folder: {exc}", 409) from exc

    case_id = secrets.token_hex(12)
    try:
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            collision = conn.execute(
                "SELECT case_id FROM recovered_channel_cases WHERE source_channel_id = ?",
                (channel_id,),
            ).fetchone()
            component_collision = _target_component_collision(conn, component)
            if collision or component_collision:
                raise RecoveredChannelError(
                    component_collision
                    or f"Recovered case {collision['case_id']} already owns this source channel",
                    409,
                )
            conn.execute(
                """
                INSERT INTO recovered_channel_cases
                    (case_id, source_channel_id, target_channel_name,
                     target_component, status, created_by)
                VALUES (?, ?, ?, ?, 'draft', ?)
                """,
                (case_id, channel_id, name, component, actor),
            )
            _insert_discovered(conn, case_id, discovered)
            _event(
                conn,
                case_id,
                "case_created",
                actor=actor,
                details={
                    "source_channel_id": channel_id,
                    "target_component": component,
                    "items": len(discovered),
                },
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise RecoveredChannelError("A case already owns this source channel", 409) from exc
    return get_case(case_id)["case"]


def list_cases() -> list[dict[str, Any]]:
    _recover_orphaned_jobs()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM recovered_channel_cases ORDER BY updated_at DESC"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            case = _case_row(row)
            counts = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN inspection_status = 'completed' THEN 1 ELSE 0 END) AS inspected,
                       SUM(CASE WHEN inspection_status = 'inconclusive' THEN 1 ELSE 0 END) AS inconclusive,
                       SUM(CASE WHEN inspection_status IN ('completed', 'inconclusive') THEN 1 ELSE 0 END) AS finished,
                       SUM(CASE WHEN organized_path IS NULL AND decision = 'approved' THEN 1 ELSE 0 END) AS approved,
                       SUM(CASE WHEN organized_path IS NOT NULL THEN 1 ELSE 0 END) AS organized
                FROM recovered_channel_items WHERE case_id = ?
                """,
                (row["case_id"],),
            ).fetchone()
            case["counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
            case["counts"]["repair_needed"] = _repair_needed_count(conn, row)
            case["repair_needed"] = case["counts"]["repair_needed"]
            result.append(case)
        return result


def get_case(case_id: str) -> dict[str, Any]:
    _recover_orphaned_jobs()
    with get_db() as conn:
        row = _require_case(conn, case_id)
        counts_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN inspection_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN inspection_status = 'inspecting' THEN 1 ELSE 0 END) AS inspecting,
                   SUM(CASE WHEN inspection_status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN inspection_status = 'inconclusive' THEN 1 ELSE 0 END) AS inconclusive,
                   SUM(CASE WHEN inspection_status IN ('completed', 'inconclusive') THEN 1 ELSE 0 END) AS finished,
                   SUM(CASE WHEN organized_path IS NULL AND health_tier IN ('clear', 'minimal') THEN 1 ELSE 0 END) AS best,
                   SUM(CASE WHEN organized_path IS NULL AND health_tier IN ('serious', 'inconclusive') THEN 1 ELSE 0 END) AS review,
                   SUM(CASE WHEN organized_path IS NULL AND decision = 'approved' THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN organized_path IS NULL AND decision = 'hold' THEN 1 ELSE 0 END) AS hold,
                   SUM(CASE WHEN organized_path IS NOT NULL THEN 1 ELSE 0 END) AS organized
            FROM recovered_channel_items WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()
        active = _active_job(conn, case_id)
        counts = {key: int(counts_row[key] or 0) for key in counts_row.keys()}
        counts["repair_needed"] = _repair_needed_count(conn, row)
        case = _case_row(row)
        case["repair_needed"] = counts["repair_needed"]
        return {
            "case": case,
            "counts": counts,
            "active_job": _job_row(active) if active else None,
        }


def update_case(
    case_id: str, data: dict[str, Any], actor: str | None = None
) -> dict[str, Any]:
    name, component = _validated_target_name(data.get("target_channel_name"))
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_case(conn, case_id)
        if _active_job(conn, case_id):
            raise RecoveredChannelError("Wait for the active job before renaming", 409)
        if conn.execute(
            "SELECT 1 FROM recovered_channel_items WHERE case_id = ? AND organized_path IS NOT NULL",
            (case_id,),
        ).fetchone():
            raise RecoveredChannelError(
                "A case cannot be renamed after it has published links", 409
            )
        component_collision = _target_component_collision(
            conn, component, exclude_case_id=case_id
        )
        if component_collision:
            raise RecoveredChannelError(component_collision, 409)
        folder = TARGET_DIR / component
        if os.path.lexists(folder):
            try:
                folder_stat = os.lstat(folder)
                if stat.S_ISLNK(folder_stat.st_mode) or not stat.S_ISDIR(
                    folder_stat.st_mode
                ):
                    raise RecoveredChannelError(
                        "The recovered target path is not a real directory", 409
                    )
                if any(folder.iterdir()):
                    raise RecoveredChannelError(
                        "The requested target folder is not empty", 409
                    )
            except RecoveredChannelError:
                raise
            except OSError as exc:
                raise RecoveredChannelError(
                    f"Could not validate target folder: {exc}", 409
                ) from exc
        try:
            conn.execute(
                """
                UPDATE recovered_channel_cases
                SET target_channel_name = ?, target_component = ?, updated_at = CURRENT_TIMESTAMP
                WHERE case_id = ?
                """,
                (name, component, case_id),
            )
        except sqlite3.IntegrityError as exc:
            raise RecoveredChannelError(
                "Another recovered case already owns that target name", 409
            ) from exc
        _event(
            conn,
            case_id,
            "case_renamed",
            actor=actor,
            details={"target_channel_name": name, "target_component": component},
        )
        conn.commit()
    return get_case(case_id)["case"]


def list_items(
    case_id: str,
    page: int = 1,
    per_page: int = 100,
    health: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    page = max(1, int(page))
    per_page = max(1, min(_MAX_PAGE_SIZE, int(per_page)))
    clauses = ["recovered_channel_items.case_id = ?"]
    values: list[object] = [case_id]
    if health:
        allowed = {"clear", "minimal", "serious", "inconclusive"}
        if health not in allowed:
            raise RecoveredChannelError("Unknown health filter")
        clauses.append("health_tier = ?")
        values.append(health)
    if decision:
        if decision not in {"pending", "approved", "hold"}:
            raise RecoveredChannelError("Unknown decision filter")
        clauses.append("decision = ?")
        values.append(decision)
    where = " AND ".join(clauses)
    with get_db() as conn:
        _require_case(conn, case_id)
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM recovered_channel_items WHERE {where}",
            values,
        ).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT recovered_channel_items.*,
                   recovered_channel_cases.source_channel_id AS source_channel_id,
                   recovered_channel_cases.target_component AS target_component,
                   videos.status AS recovered_owner_status,
                   videos.symlink AS recovered_owner_symlink
            FROM recovered_channel_items
            JOIN recovered_channel_cases USING (case_id)
            LEFT JOIN videos
              ON videos.video_id = recovered_channel_items.video_id
            WHERE {where}
            ORDER BY recovered_channel_items.video_id,
                     recovered_channel_items.source_path LIMIT ? OFFSET ?
            """,
            (*values, per_page, (page - 1) * per_page),
        ).fetchall()
    return {
        "items": [_item_row(row) for row in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page,
    }


def _expected_duration(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RecoveredChannelError("expected_duration_seconds must be positive")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise RecoveredChannelError("expected_duration_seconds must be positive") from None
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 31 * 24 * 3600:
        raise RecoveredChannelError(
            "expected_duration_seconds must be positive and no more than 31 days"
        )
    return parsed


def _fingerprint_error(row: sqlite3.Row, channel_id: str) -> str | None:
    path = Path(row["source_path"])
    expected_root = SOURCE_DIR / channel_id
    try:
        path.relative_to(expected_root)
        current = os.lstat(path)
    except (ValueError, OSError):
        return "Source file is missing or outside the selected channel"
    if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
        return "Source entry is no longer a real regular file"
    if extract_id_from_filename(path.name) != row["video_id"]:
        return "Source filename no longer contains the exact video ID"
    actual = (
        current.st_size,
        current.st_dev,
        current.st_ino,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    expected = tuple(
        row[name] for name in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns")
    )
    if actual != expected:
        return "Source fingerprint changed; inspect this file again"
    return None


def update_item(
    case_id: str,
    item_id: str,
    data: dict[str, Any],
    actor: str | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RecoveredChannelError("Request body must be an object")
    allowed_keys = {"decision", "expected_duration_seconds"}
    if not set(data).issubset(allowed_keys) or not data:
        raise RecoveredChannelError("Only decision and expected_duration_seconds may be changed")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case = _require_case(conn, case_id)
        row = _require_item(conn, case_id, item_id)
        if _active_job(conn, case_id):
            raise RecoveredChannelError("Wait for the active job before editing items", 409)
        if row["organized_path"]:
            raise RecoveredChannelError("Organized items are immutable history", 409)
        if "expected_duration_seconds" in data:
            expected = _expected_duration(data["expected_duration_seconds"])
            conn.execute(
                """
                UPDATE recovered_channel_items
                SET expected_duration_seconds = ?, inspection_status = 'pending',
                    health_tier = NULL, diagnosis_json = NULL, sha256 = NULL,
                    decision = 'pending', error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE item_id = ?
                """,
                (expected, item_id),
            )
            _event(
                conn,
                case_id,
                "expected_duration_changed",
                actor=actor,
                item_id=item_id,
                details={"expected_duration_seconds": expected},
            )
        if "decision" in data:
            decision = data["decision"]
            if decision not in {"pending", "approved", "hold"}:
                raise RecoveredChannelError("decision must be pending, approved, or hold")
            current = _require_item(conn, case_id, item_id)
            if decision == "approved":
                if (
                    current["inspection_status"] != "completed"
                    or current["health_tier"] not in {"clear", "minimal", "serious"}
                    or not isinstance(current["sha256"], str)
                    or _SHA256_RE.fullmatch(current["sha256"]) is None
                ):
                    raise RecoveredChannelError(
                        "Only a completed, checksum-bound inspection can be approved", 409
                    )
                error = _fingerprint_error(current, case["source_channel_id"])
                if error:
                    raise RecoveredChannelError(error, 409)
                duplicate = conn.execute(
                    """
                    SELECT item_id FROM recovered_channel_items
                    WHERE case_id = ? AND video_id = ? AND decision = 'approved'
                      AND organized_path IS NULL AND item_id != ?
                    LIMIT 1
                    """,
                    (case_id, current["video_id"], item_id),
                ).fetchone()
                if duplicate:
                    raise RecoveredChannelError(
                        "Another candidate with this exact video ID is already approved",
                        409,
                    )
            try:
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET decision = ?, error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                    """,
                    (decision, item_id),
                )
            except sqlite3.IntegrityError as exc:
                raise RecoveredChannelError(
                    "Another candidate with this exact video ID is already approved",
                    409,
                ) from exc
            _event(
                conn,
                case_id,
                "decision_changed",
                actor=actor,
                item_id=item_id,
                details={"decision": decision},
            )
        conn.execute(
            "UPDATE recovered_channel_cases SET updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        conn.commit()
        return _item_row(_require_item(conn, case_id, item_id))


def bulk_decision(
    case_id: str,
    item_ids: list[str],
    decision: str,
    actor: str | None = None,
) -> int:
    if (
        not isinstance(item_ids, list)
        or not item_ids
        or len(item_ids) > _MAX_BATCH
        or any(not isinstance(item_id, str) or not item_id for item_id in item_ids)
    ):
        raise RecoveredChannelError(
            f"item_ids must contain 1 to {_MAX_BATCH} item IDs"
        )
    if len(set(item_ids)) != len(item_ids):
        raise RecoveredChannelError("item_ids must not contain duplicates")
    if decision not in {"pending", "approved", "hold"}:
        raise RecoveredChannelError("decision must be pending, approved, or hold")
    updated = 0
    # Validate the whole batch before applying any decision.
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case = _require_case(conn, case_id)
        if _active_job(conn, case_id):
            raise RecoveredChannelError("Wait for the active job before editing items", 409)
        rows = [
            _require_item(conn, case_id, item_id)
            for item_id in item_ids
        ]
        if decision == "approved":
            duplicate_ids = [
                video_id
                for video_id, count in Counter(row["video_id"] for row in rows).items()
                if count > 1
            ]
            if duplicate_ids:
                raise RecoveredChannelError(
                    "Approve only one physical candidate for each exact video ID",
                    409,
                )
        for row in rows:
            if row["organized_path"]:
                raise RecoveredChannelError("Organized items are immutable history", 409)
            if decision == "approved":
                if (
                    row["inspection_status"] != "completed"
                    or row["health_tier"] not in {"clear", "minimal", "serious"}
                    or not isinstance(row["sha256"], str)
                    or _SHA256_RE.fullmatch(row["sha256"]) is None
                ):
                    raise RecoveredChannelError(
                        "Every approved item needs a completed checksum-bound inspection",
                        409,
                    )
                error = _fingerprint_error(row, case["source_channel_id"])
                if error:
                    raise RecoveredChannelError(error, 409)
                duplicate = conn.execute(
                    """
                    SELECT item_id FROM recovered_channel_items
                    WHERE case_id = ? AND video_id = ? AND decision = 'approved'
                      AND organized_path IS NULL AND item_id NOT IN ({})
                    LIMIT 1
                    """.format(",".join("?" for _ in item_ids)),
                    (case_id, row["video_id"], *item_ids),
                ).fetchone()
                if duplicate:
                    raise RecoveredChannelError(
                        "Another candidate with this exact video ID is already approved",
                        409,
                    )
        for row in rows:
            try:
                conn.execute(
                    "UPDATE recovered_channel_items SET decision = ?, error = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_id = ?",
                    (decision, row["item_id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise RecoveredChannelError(
                    "Another candidate with this exact video ID is already approved",
                    409,
                ) from exc
            updated += 1
        _event(
            conn,
            case_id,
            "bulk_decision_changed",
            actor=actor,
            details={"decision": decision, "item_ids": item_ids},
        )
        conn.execute(
            "UPDATE recovered_channel_cases SET updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        conn.commit()
    return updated


def _classify_diagnosis(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    decode = source.get("decode") if isinstance(source.get("decode"), dict) else {}
    streams = decode.get("streams") if isinstance(decode.get("streams"), list) else []
    findings: list[dict[str, Any]] = []
    serious = False
    represented_messages: set[str] = set()
    decoded_endpoints: list[tuple[str, object, float]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_type = stream.get("type") or "media"
        stream_index = stream.get("index")
        messages = [str(message) for message in stream.get("messages") or []]
        if not stream.get("clean", False):
            serious = True
            finding = {
                "code": "decode_error",
                "severity": "serious",
                "stream_type": stream_type,
                "stream_index": stream_index,
                "message": f"{stream_type.title()} stream {stream_index} did not decode cleanly.",
                "details": messages,
            }
            findings.append(finding)
            represented_messages.update(messages)
        actual = stream.get("decoded_duration_seconds")
        declared = stream.get("declared_duration_seconds")
        try:
            positive_endpoint = math.isfinite(float(actual)) and float(actual) > 0
        except (TypeError, ValueError):
            positive_endpoint = False
        if not positive_endpoint:
            serious = True
            findings.append({
                "code": "no_positive_decoded_endpoint",
                "severity": "serious",
                "stream_type": stream_type,
                "stream_index": stream_index,
                "message": (
                    f"{stream_type.title()} stream {stream_index} has no positive "
                    "decoded endpoint; completeness cannot be established."
                ),
                "actual_seconds": actual,
            })
        else:
            decoded_endpoints.append((stream_type, stream_index, float(actual)))
        if salvage._materially_short(actual, declared):
            serious = True
            findings.append({
                "code": "declared_duration_shortfall",
                "severity": "serious",
                "stream_type": stream_type,
                "stream_index": stream_index,
                "message": f"{stream_type.title()} stream {stream_index} ends materially before its declared duration.",
                "actual_seconds": actual,
                "expected_seconds": declared,
            })
    video_endpoints = [item for item in decoded_endpoints if item[0] == "video"]
    audio_endpoints = [item for item in decoded_endpoints if item[0] == "audio"]
    if video_endpoints and audio_endpoints:
        primary_video = video_endpoints[0]
        for audio in audio_endpoints:
            shorter, longer = sorted((primary_video[2], audio[2]))
            if not salvage._materially_short(shorter, longer):
                continue
            serious = True
            findings.append({
                "code": "stream_endpoint_divergence",
                "severity": "serious",
                "stream_type": "audio",
                "stream_index": audio[1],
                "message": (
                    "Primary video and audio decoded endpoints differ materially; "
                    "one stream may cut off early."
                ),
                "video_seconds": primary_video[2],
                "audio_seconds": audio[2],
            })
    expected = result.get("expected_duration_seconds")
    readable = source.get("readable_duration_seconds")
    if salvage._materially_short(readable, expected):
        serious = True
        findings.append({
            "code": "expected_duration_shortfall",
            "severity": "serious",
            "message": "Decoded media ends materially before the operator-supplied duration.",
            "actual_seconds": readable,
            "expected_seconds": expected,
        })
    for warning in source.get("warnings") or []:
        warning_text = str(warning)
        if warning_text in represented_messages:
            continue
        findings.append({
            "code": "diagnostic_warning",
            "severity": "minimal" if not serious else "serious",
            "message": warning_text,
        })
    if serious:
        return "serious", findings
    if findings or result.get("assessment") == "warning":
        return "minimal", findings
    return "clear", findings


def _job_cancel_requested(job_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM recovered_channel_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return bool(row and row["cancel_requested"])


def _launch(job_id: str, target) -> None:
    def runner() -> None:
        try:
            target(job_id)
        except Exception as exc:  # durable worker boundary
            _persist_worker_failure(job_id, exc)
        finally:
            with _runtime_lock:
                _threads.pop(job_id, None)
                _active_salvage_jobs.pop(job_id, None)

    thread = threading.Thread(
        target=runner,
        daemon=True,
        name=f"recovered-channel-{job_id[:8]}",
    )
    with _runtime_lock:
        _threads[job_id] = thread
    try:
        thread.start()
    except Exception as exc:
        with _runtime_lock:
            _threads.pop(job_id, None)
        _persist_worker_failure(job_id, exc)
        raise RecoveredChannelError(
            f"Could not start the recovered-channel worker: {exc}", 503
        ) from exc


def _persist_worker_failure(job_id: str, error: Exception) -> None:
    message = (str(error) or error.__class__.__name__)[:2000]
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT case_id, kind, status FROM recovered_channel_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] in _TERMINAL_JOB_STATES:
                return
            conn.execute(
                """
                UPDATE recovered_channel_jobs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE job_id = ?
                """,
                (_now(), message, job_id),
            )
            if row["kind"] == "inspect":
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET inspection_status = 'pending', decision = 'pending',
                        error = 'Inspection worker stopped unexpectedly; resume the case',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE case_id = ? AND inspection_status = 'inspecting'
                    """,
                    (row["case_id"],),
                )
            conn.execute(
                "UPDATE recovered_channel_cases SET status = 'review', "
                "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
                (row["case_id"],),
            )
            _event(
                conn,
                row["case_id"],
                "worker_failed",
                details={"job_id": job_id, "kind": row["kind"], "error": message},
            )
            conn.commit()
        log(f"❌ [RECOVERED CHANNEL] Worker {job_id} failed: {message}")
    except Exception as persist_error:  # avoid hiding the original thread failure
        log(
            "❌ [RECOVERED CHANNEL] Could not persist worker failure "
            f"for {job_id}: {persist_error}; original error: {message}"
        )


def start_inspection(
    case_id: str,
    rescan: bool = False,
    actor: str | None = None,
    background: bool = True,
) -> dict[str, Any]:
    _recover_orphaned_jobs()
    _sync_case_inventory(case_id)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _require_case(conn, case_id)
        if _active_job(conn, case_id):
            raise RecoveredChannelError("This case already has an active job", 409)
        if rescan:
            rows = conn.execute(
                "SELECT item_id FROM recovered_channel_items "
                "WHERE case_id = ? AND organized_path IS NULL ORDER BY video_id, source_path",
                (case_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT item_id FROM recovered_channel_items
                WHERE case_id = ? AND organized_path IS NULL
                  AND inspection_status != 'completed'
                ORDER BY video_id, source_path
                """,
                (case_id,),
            ).fetchall()
        item_ids = [row["item_id"] for row in rows]
        if not item_ids:
            raise RecoveredChannelError("No items currently require inspection", 409)
        job_id = secrets.token_hex(12)
        try:
            conn.execute(
                """
                INSERT INTO recovered_channel_jobs
                    (job_id, case_id, kind, status, total, payload_json)
                VALUES (?, ?, 'inspect', 'queued', ?, ?)
                """,
                (job_id, case_id, len(item_ids), _json({"item_ids": item_ids})),
            )
        except sqlite3.IntegrityError as exc:
            raise RecoveredChannelError(
                "This case already has an active job", 409
            ) from exc
        placeholders = ",".join("?" for _ in item_ids)
        conn.execute(
            f"""
            UPDATE recovered_channel_items
            SET inspection_status = 'pending', decision = 'pending', error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE case_id = ? AND item_id IN ({placeholders})
            """,
            (case_id, *item_ids),
        )
        conn.execute(
            "UPDATE recovered_channel_cases SET status = 'inspecting', "
            "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        _event(
            conn,
            case_id,
            "inspection_started",
            actor=actor,
            details={"job_id": job_id, "items": len(item_ids), "rescan": bool(rescan)},
        )
        conn.commit()
        job = _job_row(
            conn.execute(
                "SELECT * FROM recovered_channel_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        )
    if background:
        _launch(job_id, run_inspection_job)
    else:
        try:
            run_inspection_job(job_id)
        except Exception as exc:
            _persist_worker_failure(job_id, exc)
        job = get_job(case_id, job_id)
    return job


def _wait_for_salvage_job(parent_job_id: str, child_job_id: str) -> dict[str, Any]:
    while True:
        child = salvage.get_job(child_job_id)
        if child is None:
            raise RecoveredChannelError("Media diagnosis state disappeared", 500)
        if child["status"] in {"completed", "failed", "cancelled"}:
            return child
        if _job_cancel_requested(parent_job_id):
            salvage.cancel_job(child_job_id)
        time.sleep(0.2)


def _terminalize_prestart_cancel(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> None:
    """Make a legacy/pre-start cancelling row terminal and retryable."""
    conn.execute(
        "UPDATE recovered_channel_jobs SET status = 'cancelled', "
        "cancel_requested = 1, completed_at = ? "
        "WHERE job_id = ? AND status = 'cancelling'",
        (_now(), row["job_id"]),
    )
    if row["kind"] == "inspect":
        conn.execute(
            """
            UPDATE recovered_channel_items
            SET inspection_status = 'pending', decision = 'pending',
                error = 'Inspection cancelled before it started',
                updated_at = CURRENT_TIMESTAMP
            WHERE case_id = ? AND inspection_status = 'inspecting'
            """,
            (row["case_id"],),
        )
    conn.execute(
        "UPDATE recovered_channel_cases SET status = 'review', "
        "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
        (row["case_id"],),
    )
    _event(
        conn,
        row["case_id"],
        "job_cancelled",
        details={"job_id": row["job_id"], "kind": row["kind"], "before_start": True},
    )
    conn.commit()


def run_inspection_job(job_id: str) -> None:
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM recovered_channel_jobs WHERE job_id = ? AND kind = 'inspect'",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RecoveredChannelError("Inspection job not found", 404)
        if row["status"] == "cancelling":
            _terminalize_prestart_cancel(conn, row)
            return
        if row["status"] not in {"queued", "interrupted"}:
            return
        payload = _decode_json(row["payload_json"], {})
        item_ids = payload.get("item_ids") if isinstance(payload, dict) else None
        if not isinstance(item_ids, list):
            item_ids = []
        case_id = row["case_id"]
        cursor = conn.execute(
            "UPDATE recovered_channel_jobs SET status = 'running', started_at = ?, "
            "completed_at = NULL, error = NULL WHERE job_id = ? "
            "AND status IN ('queued', 'interrupted')",
            (_now(), job_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return
        conn.commit()

    success_count = 0
    fail_count = 0
    cancelled = False
    for position, item_id in enumerate(item_ids, start=1):
        if _job_cancel_requested(job_id):
            cancelled = True
            break
        with get_db() as conn:
            case = _require_case(conn, case_id)
            item = _require_item(conn, case_id, item_id)
            if item["organized_path"]:
                conn.execute(
                    "UPDATE recovered_channel_jobs SET current = ? WHERE job_id = ?",
                    (position, job_id),
                )
                conn.commit()
                continue
            fingerprint_error = _fingerprint_error(item, case["source_channel_id"])
            if fingerprint_error:
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET inspection_status = 'inconclusive', health_tier = 'inconclusive',
                        decision = 'pending', error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                    """,
                    (fingerprint_error, item_id),
                )
                conn.execute(
                    "UPDATE recovered_channel_jobs SET current = ?, fail_count = fail_count + 1 "
                    "WHERE job_id = ?",
                    (position, job_id),
                )
                conn.commit()
                fail_count += 1
                continue
            conn.execute(
                """
                UPDATE recovered_channel_items
                SET inspection_status = 'inspecting', decision = 'pending', error = NULL,
                    updated_at = CURRENT_TIMESTAMP WHERE item_id = ?
                """,
                (item_id,),
            )
            conn.commit()
            source_path = item["source_path"]
            expected = item["expected_duration_seconds"]

        child: dict[str, Any] | None = None
        while not _job_cancel_requested(job_id):
            child, message, status = salvage.start_job(
                source_path,
                "diagnose",
                expected_duration_seconds=expected,
            )
            if child is not None:
                break
            if status == 429:
                time.sleep(0.5)
                continue
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET inspection_status = 'inconclusive', health_tier = 'inconclusive',
                        decision = 'pending', error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                    """,
                    (message or "Could not start media diagnosis", item_id),
                )
                conn.commit()
            fail_count += 1
            break
        if child is None:
            if _job_cancel_requested(job_id):
                cancelled = True
                break
            with get_db() as conn:
                conn.execute(
                    "UPDATE recovered_channel_jobs SET current = ?, fail_count = ? WHERE job_id = ?",
                    (position, fail_count, job_id),
                )
                conn.commit()
            continue

        child_id = child["id"]
        with _runtime_lock:
            _active_salvage_jobs[job_id] = child_id
        child = _wait_for_salvage_job(job_id, child_id)
        with _runtime_lock:
            _active_salvage_jobs.pop(job_id, None)
        if child["status"] == "completed" and isinstance(child.get("result"), dict):
            diagnosis = child["result"]
            health, findings = _classify_diagnosis(diagnosis)
            persisted = dict(diagnosis)
            persisted["workflow_findings"] = findings
            source = diagnosis.get("source") if isinstance(diagnosis.get("source"), dict) else {}
            checksum = source.get("sha256")
            if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
                health = "inconclusive"
                error = "Diagnosis did not return a trustworthy SHA-256"
                checksum = None
                fail_count += 1
            else:
                error = None
                success_count += 1
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET inspection_status = ?, health_tier = ?, diagnosis_json = ?,
                        sha256 = ?, decision = 'pending', error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ?
                    """,
                    (
                        "completed" if checksum else "inconclusive",
                        health,
                        _json(persisted),
                        checksum,
                        error,
                        item_id,
                    ),
                )
                _event(
                    conn,
                    case_id,
                    "item_inspected",
                    item_id=item_id,
                    details={"health_tier": health, "sha256": checksum},
                )
                conn.commit()
        else:
            if child["status"] == "cancelled":
                cancelled = True
                error = "Inspection cancelled"
                status_value = "pending"
                health = None
            else:
                error = child.get("error") or "Media diagnosis failed"
                status_value = "inconclusive"
                health = "inconclusive"
                fail_count += 1
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE recovered_channel_items
                    SET inspection_status = ?, health_tier = ?, decision = 'pending',
                        error = ?, updated_at = CURRENT_TIMESTAMP WHERE item_id = ?
                    """,
                    (status_value, health, error, item_id),
                )
                conn.commit()
        with get_db() as conn:
            conn.execute(
                """
                UPDATE recovered_channel_jobs
                SET current = ?, success_count = ?, fail_count = ? WHERE job_id = ?
                """,
                (position, success_count, fail_count, job_id),
            )
            conn.commit()
        if cancelled:
            break

    final_status = "cancelled" if cancelled else "completed"
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE recovered_channel_jobs
            SET status = ?, success_count = ?, fail_count = ?, completed_at = ?,
                error = ? WHERE job_id = ? AND status IN ('running', 'cancelling')
            """,
            (
                final_status,
                success_count,
                fail_count,
                _now(),
                f"{fail_count} item(s) need review" if fail_count and not cancelled else None,
                job_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return
        conn.execute(
            "UPDATE recovered_channel_cases SET status = 'review', "
            "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        _event(
            conn,
            case_id,
            "inspection_finished",
            details={
                "job_id": job_id,
                "status": final_status,
                "success_count": success_count,
                "fail_count": fail_count,
            },
        )
        conn.commit()


def get_job(case_id: str, job_id: str) -> dict[str, Any]:
    _recover_orphaned_jobs()
    with get_db() as conn:
        _require_case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM recovered_channel_jobs WHERE case_id = ? AND job_id = ?",
            (case_id, job_id),
        ).fetchone()
        if row is None:
            raise RecoveredChannelError("Recovered channel job not found", 404)
        return _job_row(row)


def cancel_job(
    case_id: str, job_id: str, actor: str | None = None
) -> dict[str, Any]:
    with get_db() as conn:
        _require_case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM recovered_channel_jobs WHERE case_id = ? AND job_id = ?",
            (case_id, job_id),
        ).fetchone()
        if row is None:
            raise RecoveredChannelError("Recovered channel job not found", 404)
        if row["status"] in _TERMINAL_JOB_STATES:
            return _job_row(row)
        cursor = conn.execute(
            """
            UPDATE recovered_channel_jobs
            SET cancel_requested = 1,
                status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END,
                completed_at = CASE WHEN status = 'queued' THEN ? ELSE completed_at END
            WHERE case_id = ? AND job_id = ?
              AND status IN ('queued', 'running', 'cancelling')
            RETURNING *
            """,
            (_now(), case_id, job_id),
        )
        updated = cursor.fetchone()
        if updated is None:
            conn.rollback()
            fresh = conn.execute(
                "SELECT * FROM recovered_channel_jobs WHERE case_id = ? AND job_id = ?",
                (case_id, job_id),
            ).fetchone()
            if fresh is None:
                raise RecoveredChannelError("Recovered channel job not found", 404)
            return _job_row(fresh)
        terminal_cancel = updated["status"] == "cancelled"
        if terminal_cancel:
            conn.execute(
                "UPDATE recovered_channel_cases SET status = 'review', "
                "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
                (case_id,),
            )
        _event(
            conn,
            case_id,
            "job_cancelled" if terminal_cancel else "job_cancel_requested",
            actor=actor,
            details={"job_id": job_id, "kind": row["kind"]},
        )
        conn.commit()
        if terminal_cancel:
            return _job_row(updated)
    with _runtime_lock:
        child_id = _active_salvage_jobs.get(job_id)
    if child_id:
        salvage.cancel_job(child_id)
    return get_job(case_id, job_id)


def _target_folder_error(component: str, *, allow_missing: bool = True) -> str | None:
    try:
        root_stat = os.lstat(TARGET_DIR)
    except FileNotFoundError:
        return "Target root is unavailable; verify the configured mount"
    except OSError as exc:
        return f"Target root is unavailable: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        return "Target root must be a real directory"
    folder = TARGET_DIR / component
    if not os.path.lexists(folder):
        return None if allow_missing else "Target folder does not exist"
    try:
        folder_stat = os.lstat(folder)
    except OSError as exc:
        return f"Target folder is unavailable: {exc}"
    if stat.S_ISLNK(folder_stat.st_mode) or not stat.S_ISDIR(folder_stat.st_mode):
        return "Target folder must be a real directory"
    if not path_in_allowed_tree(folder, (TARGET_DIR,)):
        return "Target folder escaped TARGET_DIR"
    return None


def _destination_state(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        entry = os.lstat(path)
        return {
            "mode": stat.S_IFMT(entry.st_mode),
            "device": entry.st_dev,
            "inode": entry.st_ino,
            "target": os.readlink(path) if stat.S_ISLNK(entry.st_mode) else None,
        }
    except OSError:
        return {"unreadable": True}


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _open_managed_component(
    managed_root: Path,
    component: str,
    *,
    create: bool,
) -> tuple[int, int]:
    """Open and pin one configured managed root plus a validated component."""
    try:
        root_fd = os.open(managed_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise RecoveredChannelError(f"Target root is unavailable: {exc}", 409) from exc
    component_fd: int | None = None
    try:
        if create:
            try:
                os.mkdir(component, mode=0o755, dir_fd=root_fd)
            except FileExistsError:
                pass
        component_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=root_fd)
        _assert_pinned_managed_root(
            managed_root, root_fd, component_fd, component
        )
        return root_fd, component_fd
    except OSError as exc:
        if component_fd is not None:
            os.close(component_fd)
        os.close(root_fd)
        raise RecoveredChannelError(
            f"Recovered target folder is unavailable or unsafe: {exc}", 409
        ) from exc
    except Exception:
        if component_fd is not None:
            os.close(component_fd)
        os.close(root_fd)
        raise


def _assert_pinned_managed_root(
    managed_root: Path, root_fd: int, component_fd: int, component: str
) -> None:
    """Require configured names to still identify both open directories."""
    try:
        open_root = os.fstat(root_fd)
        named_root = os.stat(managed_root, follow_symlinks=False)
        open_component = os.fstat(component_fd)
        named_component = os.stat(
            component, dir_fd=root_fd, follow_symlinks=False
        )
    except OSError as exc:
        raise RecoveredChannelError(
            "Recovered target directories changed during publication", 409
        ) from exc
    if (
        not stat.S_ISDIR(open_root.st_mode)
        or not stat.S_ISDIR(named_root.st_mode)
        or not stat.S_ISDIR(open_component.st_mode)
        or not stat.S_ISDIR(named_component.st_mode)
        or (open_root.st_dev, open_root.st_ino)
        != (named_root.st_dev, named_root.st_ino)
        or (open_component.st_dev, open_component.st_ino)
        != (named_component.st_dev, named_component.st_ino)
    ):
        raise RecoveredChannelError(
            "Recovered target directories changed during publication", 409
        )


def _link_state_at(directory_fd: int, filename: str) -> dict[str, Any] | None:
    try:
        entry = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return {
        "mode": stat.S_IFMT(entry.st_mode),
        "device": entry.st_dev,
        "inode": entry.st_ino,
        "target": (
            os.readlink(filename, dir_fd=directory_fd)
            if stat.S_ISLNK(entry.st_mode)
            else None
        ),
    }


def _read_managed_link_state(
    managed_root: Path, component: str, filename: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read link health through no-follow directory descriptors.

    ``parent_status`` is ``missing`` only when the channel component is absent
    beneath an existing configured root and can be recreated by the guarded
    repair endpoint. A missing configured root can indicate a mount outage and
    is unsafe; it must never be recreated on underlying container storage.
    """
    root_fd: int | None = None
    component_fd: int | None = None
    try:
        root_fd = os.open(managed_root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return None, "unsafe"
    except OSError:
        return None, "unsafe"
    try:
        try:
            component_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            return None, "missing"
        except OSError:
            # ELOOP/ENOTDIR/permission errors include symlinked or otherwise
            # unsafe components and must never offer a repair action.
            return None, "unsafe"
        try:
            _assert_pinned_managed_root(
                managed_root, root_fd, component_fd, component
            )
        except RecoveredChannelError:
            return None, "unsafe"
        try:
            return _link_state_at(component_fd, filename), None
        except OSError:
            return None, "unsafe"
    finally:
        if component_fd is not None:
            os.close(component_fd)
        os.close(root_fd)


def _remove_created_symlink_at(
    directory_fd: int,
    filename: str,
    expected_target: str,
    expected_identity: tuple[int, int],
) -> None:
    """Rollback only the exact symlink inode created by this operation."""
    try:
        state = _link_state_at(directory_fd, filename)
        if (
            state is None
            or state["mode"] != stat.S_IFLNK
            or (state["device"], state["inode"]) != expected_identity
            or state["target"] != expected_target
        ):
            log(
                "⛔ Refusing recovered-link rollback because its pinned entry changed: "
                f"{filename}"
            )
            return
        os.unlink(filename, dir_fd=directory_fd)
    except OSError as exc:
        log(f"❌ Could not roll back recovered link {filename}: {exc}")


def build_organization_plan(case_id: str) -> dict[str, Any]:
    _recover_orphaned_jobs()
    with get_db() as conn:
        case = _require_case(conn, case_id)
        folder_error = _target_folder_error(case["target_component"])
        rows = conn.execute(
            """
            SELECT * FROM recovered_channel_items
            WHERE case_id = ? AND decision = 'approved' AND organized_path IS NULL
            ORDER BY video_id, source_path
            """,
            (case_id,),
        ).fetchall()
        id_counts = Counter(row["video_id"] for row in rows)
        plan_items: list[dict[str, Any]] = []
        token_items: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            destination = TARGET_DIR / case["target_component"] / (
                row["video_id"] + row["suffix"]
            )
            error: str | None = folder_error
            if error is None and id_counts[row["video_id"]] > 1:
                error = "Multiple approved source files have this exact video ID; approve only one"
            if error is None and (
                row["inspection_status"] != "completed"
                or row["health_tier"] not in {"clear", "minimal", "serious"}
                or not isinstance(row["sha256"], str)
                or _SHA256_RE.fullmatch(row["sha256"]) is None
            ):
                error = "Item does not have a completed checksum-bound inspection"
            if error is None:
                error = _fingerprint_error(row, case["source_channel_id"])
            destination_state = _destination_state(destination)
            try:
                expected_link_target = HOST_SOURCE_BASE / Path(
                    row["source_path"]
                ).relative_to(SOURCE_DIR)
            except ValueError:
                expected_link_target = None
            crash_left_expected_link = bool(
                expected_link_target is not None
                and destination_state
                and destination_state.get("mode") == stat.S_IFLNK
                and destination_state.get("target") == str(expected_link_target)
            )
            if (
                error is None
                and destination_state is not None
                and not crash_left_expected_link
            ):
                error = "Destination already exists; it will never be overwritten"
            owner = conn.execute(
                "SELECT video_id, symlink, status FROM videos WHERE video_id = ?",
                (row["video_id"],),
            ).fetchone()
            if error is None and owner is not None:
                error = "An existing database record already owns this video ID"
            item = {
                "item_id": row["item_id"],
                "video_id": row["video_id"],
                "source_path": row["source_path"],
                "destination_path": str(destination),
                "link_target": (
                    str(expected_link_target)
                    if expected_link_target is not None
                    else None
                ),
                "sha256": row["sha256"],
                "crash_left_expected_link": crash_left_expected_link,
                "ready": error is None,
                "conflict": error,
            }
            plan_items.append(item)
            if error:
                conflicts.append(item)
            token_items.append({
                **item,
                "fingerprint": [
                    row[name]
                    for name in ("size_bytes", "device", "inode", "mtime_ns", "ctime_ns")
                ],
                "destination_state": destination_state,
                "database_owner": dict(owner) if owner else None,
            })
        token_payload = {
            "schema": 1,
            "case_id": case_id,
            "source_channel_id": case["source_channel_id"],
            "target_component": case["target_component"],
            "items": token_items,
        }
        token = hashlib.sha256(_json(token_payload).encode("utf-8")).hexdigest()
        return {
            "token": token,
            "plan_token": token,
            "target_folder_path": str(TARGET_DIR / case["target_component"]),
            "items": plan_items,
            "conflicts": conflicts,
            "ready_count": sum(1 for item in plan_items if item["ready"]),
            "approved_count": len(plan_items),
            "source_immutable": True,
        }


def start_organization(
    case_id: str,
    plan_token: str,
    actor: str | None = None,
    background: bool = True,
) -> dict[str, Any]:
    if not isinstance(plan_token, str) or _SHA256_RE.fullmatch(plan_token) is None:
        raise RecoveredChannelError("plan_token must be the current organization plan token")
    _recover_orphaned_jobs()
    plan = build_organization_plan(case_id)
    if not secrets.compare_digest(plan_token, plan["token"]):
        raise RecoveredChannelError("Organization plan changed; review it again", 409)
    ready_ids = [item["item_id"] for item in plan["items"] if item["ready"]]
    if not ready_ids:
        raise RecoveredChannelError("The plan has no conflict-free approved items", 409)
    with get_db() as conn:
        # Serialize the final plan/rename check with every case editor and job
        # starter. The public plan is intentionally built before this short
        # transaction because it may stat slow network mounts.
        conn.execute("BEGIN IMMEDIATE")
        case = _require_case(conn, case_id)
        if str(TARGET_DIR / case["target_component"]) != plan["target_folder_path"]:
            raise RecoveredChannelError(
                "Organization plan changed; review it again", 409
            )
        if _active_job(conn, case_id):
            raise RecoveredChannelError("This case already has an active job", 409)
        current_rows = conn.execute(
            """
            SELECT * FROM recovered_channel_items
            WHERE case_id = ? AND decision = 'approved' AND organized_path IS NULL
            ORDER BY video_id, source_path
            """,
            (case_id,),
        ).fetchall()
        current_by_id = {row["item_id"]: row for row in current_rows}
        planned_ids = {item["item_id"] for item in plan["items"]}
        if planned_ids != set(current_by_id):
            raise RecoveredChannelError(
                "Organization plan changed; review it again", 409
            )
        for planned in plan["items"]:
            current = current_by_id[planned["item_id"]]
            destination = TARGET_DIR / case["target_component"] / (
                current["video_id"] + current["suffix"]
            )
            if (
                planned["video_id"] != current["video_id"]
                or planned["source_path"] != current["source_path"]
                or planned["destination_path"] != str(destination)
                or planned["sha256"] != current["sha256"]
            ):
                raise RecoveredChannelError(
                    "Organization plan changed; review it again", 409
                )
        job_id = secrets.token_hex(12)
        try:
            conn.execute(
                """
                INSERT INTO recovered_channel_jobs
                    (job_id, case_id, kind, status, total, fail_count, payload_json)
                VALUES (?, ?, 'organize', 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    case_id,
                    len(ready_ids),
                    len(plan["conflicts"]),
                    _json({
                        "item_ids": ready_ids,
                        "plan_token": plan_token,
                        "initial_conflicts": [
                            item["conflict"] for item in plan["conflicts"]
                        ],
                    }),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RecoveredChannelError(
                "This case already has an active job", 409
            ) from exc
        cursor = conn.execute(
            "UPDATE recovered_channel_cases SET status = 'organizing', "
            "updated_at = CURRENT_TIMESTAMP WHERE case_id = ?",
            (case_id,),
        )
        _event(
            conn,
            case_id,
            "organization_started",
            actor=actor,
            details={
                "job_id": job_id,
                "item_ids": ready_ids,
                "plan_token": plan_token,
            },
        )
        conn.commit()
        job = _job_row(
            conn.execute(
                "SELECT * FROM recovered_channel_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        )
    if background:
        _launch(job_id, run_organization_job)
    else:
        try:
            run_organization_job(job_id)
        except Exception as exc:
            _persist_worker_failure(job_id, exc)
        job = get_job(case_id, job_id)
    return job


def _hash_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if offset != size:
        raise RecoveredChannelError("Source became unreadable during checksum verification", 409)
    return digest.hexdigest()


def _organize_one(case_id: str, item_id: str) -> str:
    with get_db() as conn:
        case = _require_case(conn, case_id)
        item = _require_item(conn, case_id, item_id)
        if item["organized_path"]:
            return item["organized_path"]
        if item["decision"] != "approved" or item["inspection_status"] != "completed":
            raise RecoveredChannelError("Item is no longer approved for organization", 409)
        if not isinstance(item["sha256"], str) or _SHA256_RE.fullmatch(item["sha256"]) is None:
            raise RecoveredChannelError("Item lacks a completed inspection checksum", 409)
        duplicates = conn.execute(
            """
            SELECT COUNT(*) AS count FROM recovered_channel_items
            WHERE case_id = ? AND video_id = ? AND decision = 'approved'
              AND organized_path IS NULL
            """,
            (case_id, item["video_id"]),
        ).fetchone()["count"]
        if duplicates != 1:
            raise RecoveredChannelError("Exact video ID has multiple approved candidates", 409)
        fingerprint_error = _fingerprint_error(item, case["source_channel_id"])
        if fingerprint_error:
            raise RecoveredChannelError(fingerprint_error, 409)
        source = Path(item["source_path"])
        destination = TARGET_DIR / case["target_component"] / (
            item["video_id"] + item["suffix"]
        )
        expected_checksum = item["sha256"]

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise RecoveredChannelError(f"Could not open inspected source: {exc}", 409) from None
    reservation = None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RecoveredChannelError("Inspected source is no longer a regular file", 409)
        if not path_within(Path(f"/proc/self/fd/{fd}").resolve(strict=True), (SOURCE_DIR,)):
            raise RecoveredChannelError("Opened source escaped SOURCE_DIR", 409)
        reservation = reserve_media(
            {(opened.st_dev, opened.st_ino)},
            paths=(source,),
            owner=f"recovered channel organization {case_id}",
        )
        if _hash_fd(fd, opened.st_size) != expected_checksum:
            raise RecoveredChannelError(
                "Source SHA-256 changed after inspection; inspect it again", 409
            )
        current = os.lstat(source)
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise RecoveredChannelError("Source changed during organization", 409)

        link_target = HOST_SOURCE_BASE / source.relative_to(SOURCE_DIR)
        created_identity: tuple[int, int] | None = None
        root_fd: int | None = None
        component_fd: int | None = None
        adopted_crash_left = False
        with media_mutation_guard(
            paths=(destination,), action="publish this recovered channel link"
        ):
            try:
                root_fd, component_fd = _open_managed_component(
                    TARGET_DIR, case["target_component"], create=True
                )
                link_name = destination.name
                state = _link_state_at(component_fd, link_name)
                if state is None:
                    # dir_fd anchors publication to the verified component;
                    # symlink creation is atomic and inherently no-replace.
                    os.symlink(str(link_target), link_name, dir_fd=component_fd)
                    state = _link_state_at(component_fd, link_name)
                    if state is None:
                        raise RecoveredChannelError(
                            "Recovered link disappeared during publication", 409
                        )
                    created_identity = (state["device"], state["inode"])
                elif (
                    state["mode"] == stat.S_IFLNK
                    and state["target"] == str(link_target)
                ):
                    # A process may have died after the atomic symlink but
                    # before the SQLite transaction. The exact checksum-bound
                    # source/target pair is safe to adopt without replacement.
                    adopted_crash_left = True
                else:
                    raise RecoveredChannelError(
                        "Destination already exists and will not be overwritten", 409
                    )

                def assert_source_path_still_opened() -> None:
                    try:
                        after = os.lstat(source)
                    except OSError as exc:
                        raise RecoveredChannelError(
                            "Source disappeared before recovered link publication", 409
                        ) from exc
                    if (
                        not stat.S_ISREG(after.st_mode)
                        or (
                            after.st_dev,
                            after.st_ino,
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        )
                        != (
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_size,
                            opened.st_mtime_ns,
                            opened.st_ctime_ns,
                        )
                    ):
                        raise RecoveredChannelError(
                            "Source path changed before recovered link publication", 409
                        )

                assert_source_path_still_opened()
                _assert_pinned_managed_root(
                    TARGET_DIR, root_fd, component_fd, case["target_component"]
                )
                with get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    owner = conn.execute(
                        "SELECT symlink FROM videos WHERE video_id = ?",
                        (item["video_id"],),
                    ).fetchone()
                    if owner is not None:
                        raise RecoveredChannelError(
                            "An existing database record owns this video ID", 409
                        )
                    conn.execute(
                        """
                        INSERT INTO videos
                            (video_id, title, channel, published, symlink,
                             status, is_live, adopted_real)
                        VALUES (?, ?, ?, NULL, ?, 'recovered', 0, 0)
                        """,
                        (
                            item["video_id"],
                            item["video_id"],
                            case["target_component"],
                            str(destination),
                        ),
                    )
                    cursor = conn.execute(
                        """
                        UPDATE recovered_channel_items
                        SET organized_path = ?, organized_at = ?, error = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE case_id = ? AND item_id = ? AND decision = 'approved'
                          AND organized_path IS NULL
                        """,
                        (str(destination), _now(), case_id, item_id),
                    )
                    if cursor.rowcount != 1:
                        raise RecoveredChannelError(
                            "Item state changed before publication completed", 409
                        )
                    _event(
                        conn,
                        case_id,
                        "item_organized",
                        item_id=item_id,
                        details={
                            "source_path": str(source),
                            "destination_path": str(destination),
                            "link_target": str(link_target),
                            "sha256": expected_checksum,
                            "adopted_crash_left_link": adopted_crash_left,
                        },
                    )
                    # Recheck immediately before the durable commit. If an
                    # external writer swapped the path after os.symlink, the
                    # exact link created above is rolled back below.
                    assert_source_path_still_opened()
                    _assert_pinned_managed_root(
                        TARGET_DIR,
                        root_fd,
                        component_fd,
                        case["target_component"],
                    )
                    committed_state = _link_state_at(component_fd, link_name)
                    if (
                        committed_state is None
                        or committed_state["mode"] != stat.S_IFLNK
                        or committed_state["target"] != str(link_target)
                        or (
                            created_identity is not None
                            and (
                                committed_state["device"],
                                committed_state["inode"],
                            )
                            != created_identity
                        )
                    ):
                        raise RecoveredChannelError(
                            "Recovered link changed before database commit", 409
                        )
                    conn.commit()
            except Exception:
                if created_identity is not None and component_fd is not None:
                    _remove_created_symlink_at(
                        component_fd,
                        destination.name,
                        str(link_target),
                        created_identity,
                    )
                raise
            finally:
                if component_fd is not None:
                    os.close(component_fd)
                if root_fd is not None:
                    os.close(root_fd)
        log(
            f"🔗 [RECOVERED CHANNEL] Published immutable-source link "
            f"{destination} -> {link_target}"
        )
        return str(destination)
    except MediaReservationConflict as exc:
        raise RecoveredChannelError(str(exc), 409) from exc
    except RecoveredChannelError:
        raise
    except OSError as exc:
        raise RecoveredChannelError(
            f"Could not safely publish the recovered link: {exc}", 409
        ) from exc
    finally:
        release_media(reservation)
        os.close(fd)


def repair_recovered_link(
    case_id: str, item_id: str, actor: str | None = None
) -> dict[str, Any]:
    """Recreate one missing durable link without accepting any client path.

    The operation deliberately refuses every existing directory entry,
    including a broken or foreign symlink. Only the exact organized path and
    exact immutable source already recorded by this case are eligible.
    """
    with get_db() as conn:
        case = _require_case(conn, case_id)
        item = _require_item(conn, case_id, item_id)
        if not isinstance(item["organized_path"], str):
            raise RecoveredChannelError("This item has not been organized", 409)
        owner = conn.execute(
            "SELECT symlink, status FROM videos WHERE video_id = ?",
            (item["video_id"],),
        ).fetchone()
        if (
            owner is None
            or owner["status"] != "recovered"
            or owner["symlink"] != item["organized_path"]
        ):
            raise RecoveredChannelError(
                "Recovered database ownership no longer matches this item", 409
            )
        fingerprint_error = _fingerprint_error(item, case["source_channel_id"])
        if fingerprint_error:
            raise RecoveredChannelError(fingerprint_error, 409)
        if (
            not isinstance(item["sha256"], str)
            or _SHA256_RE.fullmatch(item["sha256"]) is None
        ):
            raise RecoveredChannelError(
                "The durable item has no checksum-bound inspection", 409
            )
        source = Path(item["source_path"])
        destination = Path(item["organized_path"])
        filename = item["video_id"] + item["suffix"]
        managed_root = next(
            (
                root
                for root in (TARGET_DIR, HIDDEN_DIR)
                if destination == root / case["target_component"] / filename
            ),
            None,
        )
        if managed_root is None:
            raise RecoveredChannelError(
                "The durable organized path is outside its recovered channel folder",
                409,
            )
        expected_checksum = item["sha256"]
        link_target = HOST_SOURCE_BASE / source.relative_to(SOURCE_DIR)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise RecoveredChannelError(f"Could not open inspected source: {exc}", 409) from exc
    reservation = None
    root_fd: int | None = None
    component_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise RecoveredChannelError("Inspected source is no longer a regular file", 409)
        try:
            opened_path = Path(f"/proc/self/fd/{source_fd}").resolve(strict=True)
        except OSError as exc:
            raise RecoveredChannelError("Could not verify inspected source", 409) from exc
        if not path_within(opened_path, (SOURCE_DIR,)):
            raise RecoveredChannelError("Opened source escaped SOURCE_DIR", 409)
        reservation = reserve_media(
            {(opened.st_dev, opened.st_ino)},
            paths=(source,),
            owner=f"recovered channel link repair {case_id}",
        )
        if _hash_fd(source_fd, opened.st_size) != expected_checksum:
            raise RecoveredChannelError(
                "Source SHA-256 changed after inspection; repair is blocked", 409
            )

        def assert_source_unchanged() -> None:
            try:
                current = os.lstat(source)
            except OSError as exc:
                raise RecoveredChannelError(
                    "Source disappeared before recovered link repair", 409
                ) from exc
            if (
                not stat.S_ISREG(current.st_mode)
                or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise RecoveredChannelError(
                    "Source changed before recovered link repair", 409
                )

        assert_source_unchanged()
        with media_mutation_guard(
            paths=(destination,), action="repair this recovered channel link"
        ):
            root_fd, component_fd = _open_managed_component(
                managed_root,
                case["target_component"],
                create=True,
            )
            if _link_state_at(component_fd, filename) is not None:
                raise RecoveredChannelError(
                    "The organized path is no longer missing; no entry was replaced",
                    409,
                )
            os.symlink(str(link_target), filename, dir_fd=component_fd)
            created = _link_state_at(component_fd, filename)
            if created is None or created["mode"] != stat.S_IFLNK:
                raise RecoveredChannelError("Recovered link creation did not persist", 409)
            created_identity = (created["device"], created["inode"])
            try:
                assert_source_unchanged()
                _assert_pinned_managed_root(
                    managed_root,
                    root_fd,
                    component_fd,
                    case["target_component"],
                )
                with get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(
                        """
                        SELECT i.source_path, i.organized_path, i.sha256,
                               i.size_bytes, i.device, i.inode, i.mtime_ns, i.ctime_ns,
                               v.symlink, v.status
                        FROM recovered_channel_items AS i
                        JOIN videos AS v ON v.video_id = i.video_id
                        WHERE i.case_id = ? AND i.item_id = ?
                        """,
                        (case_id, item_id),
                    ).fetchone()
                    if (
                        current is None
                        or current["source_path"] != str(source)
                        or current["organized_path"] != str(destination)
                        or current["symlink"] != str(destination)
                        or current["status"] != "recovered"
                        or current["sha256"] != expected_checksum
                        or tuple(
                            current[name]
                            for name in (
                                "size_bytes",
                                "device",
                                "inode",
                                "mtime_ns",
                                "ctime_ns",
                            )
                        )
                        != (
                            opened.st_size,
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_mtime_ns,
                            opened.st_ctime_ns,
                        )
                    ):
                        raise RecoveredChannelError(
                            "Recovered item ownership changed before repair commit", 409
                        )
                    owner_update = conn.execute(
                        "UPDATE videos SET last_updated = CURRENT_TIMESTAMP "
                        "WHERE video_id = ? AND symlink = ? AND status = 'recovered'",
                        (item["video_id"], str(destination)),
                    )
                    if owner_update.rowcount != 1:
                        raise RecoveredChannelError(
                            "Recovered database owner changed before repair commit",
                            409,
                        )
                    _event(
                        conn,
                        case_id,
                        "recovered_link_repaired",
                        actor=actor,
                        item_id=item_id,
                        details={
                            "destination_path": str(destination),
                            "link_target": str(link_target),
                            "source_path": str(source),
                            "sha256": expected_checksum,
                        },
                    )
                    assert_source_unchanged()
                    _assert_pinned_managed_root(
                        managed_root,
                        root_fd,
                        component_fd,
                        case["target_component"],
                    )
                    final_state = _link_state_at(component_fd, filename)
                    if (
                        final_state is None
                        or final_state["mode"] != stat.S_IFLNK
                        or final_state["target"] != str(link_target)
                        or (final_state["device"], final_state["inode"])
                        != created_identity
                    ):
                        raise RecoveredChannelError(
                            "Recovered link changed before repair commit", 409
                        )
                    conn.commit()
            except Exception:
                if created_identity is not None:
                    _remove_created_symlink_at(
                        component_fd,
                        filename,
                        str(link_target),
                        created_identity,
                    )
                raise
        log(
            f"🔗 [RECOVERED CHANNEL] Repaired missing link "
            f"{destination} -> {link_target}"
        )
    except MediaReservationConflict as exc:
        raise RecoveredChannelError(str(exc), 409) from exc
    except RecoveredChannelError:
        raise
    except OSError as exc:
        raise RecoveredChannelError(
            f"Could not safely repair the recovered link: {exc}", 409
        ) from exc
    finally:
        if component_fd is not None:
            os.close(component_fd)
        if root_fd is not None:
            os.close(root_fd)
        release_media(reservation)
        os.close(source_fd)

    with get_db() as conn:
        refreshed = conn.execute(
            """
            SELECT i.*, c.source_channel_id, c.target_component,
                   v.status AS recovered_owner_status,
                   v.symlink AS recovered_owner_symlink
            FROM recovered_channel_items AS i
            JOIN recovered_channel_cases AS c USING (case_id)
            LEFT JOIN videos AS v ON v.video_id = i.video_id
            WHERE i.case_id = ? AND i.item_id = ?
            """,
            (case_id, item_id),
        ).fetchone()
    return {
        "item": _item_row(refreshed),
        "message": "Missing recovered link recreated from its checksum-bound source",
        "link_repaired": True,
    }


def run_organization_job(job_id: str) -> None:
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM recovered_channel_jobs WHERE job_id = ? AND kind = 'organize'",
            (job_id,),
        ).fetchone()
        if row is None:
            raise RecoveredChannelError("Organization job not found", 404)
        if row["status"] == "cancelling":
            _terminalize_prestart_cancel(conn, row)
            return
        if row["status"] not in {"queued", "interrupted"}:
            return
        payload = _decode_json(row["payload_json"], {})
        item_ids = payload.get("item_ids") if isinstance(payload, dict) else []
        if not isinstance(item_ids, list):
            item_ids = []
        case_id = row["case_id"]
        cursor = conn.execute(
            "UPDATE recovered_channel_jobs SET status = 'running', started_at = ?, "
            "completed_at = NULL, error = NULL WHERE job_id = ? "
            "AND status IN ('queued', 'interrupted')",
            (_now(), job_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return
        conn.commit()
    success_count = int(row["success_count"] or 0)
    fail_count = int(row["fail_count"] or 0)
    initial_conflicts = payload.get("initial_conflicts") if isinstance(payload, dict) else []
    errors: list[str] = [
        str(error) for error in initial_conflicts or [] if error
    ]
    cancelled = False
    for position, item_id in enumerate(item_ids, start=1):
        if _job_cancel_requested(job_id):
            cancelled = True
            break
        try:
            _organize_one(case_id, item_id)
            success_count += 1
        except Exception as exc:  # continue a guarded batch after per-item failure
            fail_count += 1
            message = str(exc)[:1000] or exc.__class__.__name__
            errors.append(message)
            with get_db() as conn:
                conn.execute(
                    "UPDATE recovered_channel_items SET error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE item_id = ?",
                    (message, item_id),
                )
                _event(
                    conn,
                    case_id,
                    "item_organization_failed",
                    item_id=item_id,
                    details={"error": message},
                )
                conn.commit()
        with get_db() as conn:
            conn.execute(
                """
                UPDATE recovered_channel_jobs
                SET current = ?, success_count = ?, fail_count = ? WHERE job_id = ?
                """,
                (position, success_count, fail_count, job_id),
            )
            conn.commit()
    if cancelled:
        final_status = "cancelled"
    elif success_count and fail_count:
        final_status = "partial"
    elif fail_count:
        final_status = "failed"
    else:
        final_status = "completed"
    with get_db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM recovered_channel_items "
            "WHERE case_id = ? AND organized_path IS NULL",
            (case_id,),
        ).fetchone()["count"]
        cursor = conn.execute(
            """
            UPDATE recovered_channel_jobs
            SET status = ?, success_count = ?, fail_count = ?, completed_at = ?,
                error = ? WHERE job_id = ? AND status IN ('running', 'cancelling')
            """,
            (
                final_status,
                success_count,
                fail_count,
                _now(),
                "; ".join(errors[:3]) if errors else None,
                job_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return
        conn.execute(
            "UPDATE recovered_channel_cases SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE case_id = ?",
            ("completed" if remaining == 0 else "review", case_id),
        )
        _event(
            conn,
            case_id,
            "organization_finished",
            details={
                "job_id": job_id,
                "status": final_status,
                "success_count": success_count,
                "fail_count": fail_count,
            },
        )
        conn.commit()


def organized_recovered_video_ids() -> set[str]:
    """Return only exact IDs backed by an owned, current recovered symlink."""
    recovered: set[str] = set()
    rows = _ownership_rows(
        """
        SELECT i.*, c.source_channel_id, c.target_component,
               v.symlink AS recovered_owner_symlink,
               v.status AS recovered_owner_status
        FROM recovered_channel_items AS i
        JOIN recovered_channel_cases AS c ON c.case_id = i.case_id
        JOIN videos AS v ON v.video_id = i.video_id
        WHERE i.organized_path IS NOT NULL AND v.status = 'recovered'
        """
    )
    for row in rows:
        link_state = _item_link_state(
            row,
            source_channel_id=row["source_channel_id"],
            target_component=row["target_component"],
        )
        if link_state["link_status"] != "healthy":
            continue
        recovered.add(row["video_id"])
    return recovered


def is_recovered_media_path(filepath: str | Path) -> bool:
    """Return whether a requested path belongs to immutable recovered media."""
    requested = Path(filepath)
    try:
        requested_entry = requested.parent.resolve(strict=False) / requested.name
    except (OSError, RuntimeError, ValueError):
        requested_entry = Path(os.path.abspath(requested))
    requested_source: Path | None = None
    requested_identity: tuple[int, int] | None = None
    try:
        resolved = resolve_media_source(requested)
        if resolved is not None:
            requested_source = resolved.resolve(strict=False)
            resolved_stat = os.stat(requested_source)
            if stat.S_ISREG(resolved_stat.st_mode):
                requested_identity = (resolved_stat.st_dev, resolved_stat.st_ino)
    except (OSError, RuntimeError, ValueError):
        pass
    rows = _ownership_rows(
        """
        SELECT source_path, organized_path, device, inode
        FROM recovered_channel_items
        """
    )
    for row in rows:
        source = Path(row["source_path"])
        try:
            source_entry = source.parent.resolve(strict=False) / source.name
        except (OSError, RuntimeError, ValueError):
            source_entry = Path(os.path.abspath(source))
        if requested_entry == source_entry:
            return True
        if requested_identity == (row["device"], row["inode"]):
            return True
        if requested_source is not None:
            try:
                if requested_source == source.resolve(strict=False):
                    return True
            except (OSError, RuntimeError, ValueError):
                pass
        if isinstance(row["organized_path"], str):
            organized = Path(row["organized_path"])
            try:
                organized_entry = organized.parent.resolve(strict=False) / organized.name
            except (OSError, RuntimeError, ValueError):
                organized_entry = Path(os.path.abspath(organized))
            if requested_entry == organized_entry:
                return True
    return False


def update_recovered_paths_after_move(old_root: Path, new_root: Path) -> int:
    """Synchronize recovered DB paths after a guarded Target/Hidden migration."""
    updates: list[tuple[str, str]] = []
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT item_id, video_id, source_path, organized_path "
            "FROM recovered_channel_items "
            "WHERE organized_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            old_path = Path(row["organized_path"])
            try:
                relative = old_path.relative_to(old_root)
            except ValueError:
                continue
            new_path = new_root / relative
            # In a merge, a conflicting destination leaves the owned old link
            # in place. Never repoint history merely because some foreign entry
            # happens to exist at the would-be destination.
            if os.path.lexists(old_path):
                log(
                    f"⚠️ Recovered link was not moved because its destination conflicts: {old_path}"
                )
                continue
            try:
                source = Path(row["source_path"])
                expected_target = HOST_SOURCE_BASE / source.relative_to(SOURCE_DIR)
                if (
                    not new_path.is_symlink()
                    or new_path.name != row["video_id"] + source.suffix
                    or os.readlink(new_path) != str(expected_target)
                ):
                    log(
                        f"⛔ Refusing recovered path update for non-owned destination: {new_path}"
                    )
                    continue
            except (OSError, ValueError):
                continue
            updates.append((row["item_id"], str(new_path)))
        for item_id, new_path in updates:
            old = conn.execute(
                "SELECT organized_path, video_id FROM recovered_channel_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if old is None or not isinstance(old["organized_path"], str):
                raise RecoveredChannelMutationError(
                    "Recovered link ownership changed during channel migration"
                )
            owner_update = conn.execute(
                "UPDATE videos SET symlink = ?, last_updated = CURRENT_TIMESTAMP "
                "WHERE video_id = ? AND symlink = ? AND status = 'recovered'",
                (new_path, old["video_id"], old["organized_path"]),
            )
            if owner_update.rowcount != 1:
                raise RecoveredChannelMutationError(
                    f"Recovered database owner no longer matches {old['organized_path']}"
                )
            item_update = conn.execute(
                "UPDATE recovered_channel_items SET organized_path = ?, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE item_id = ? AND organized_path = ?",
                (new_path, item_id, old["organized_path"]),
            )
            if item_update.rowcount != 1:
                raise RecoveredChannelMutationError(
                    "Recovered item history changed during channel migration"
                )
        conn.commit()
    return len(updates)


def reset_runtime_for_tests() -> None:
    """Clear process-local worker bookkeeping after tests have stopped workers."""
    global _orphan_jobs_recovered
    with _runtime_lock:
        for parent_job_id, child_job_id in list(_active_salvage_jobs.items()):
            salvage.cancel_job(child_job_id)
            _active_salvage_jobs.pop(parent_job_id, None)
        _threads.clear()
        _orphan_jobs_recovered = False
