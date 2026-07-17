"""Availability-to-filesystem designation integration.

Online probes only establish evidence.  This service is the administrator-
confirmed boundary that binds that evidence and the channel registry revision
to :mod:`ta_organizerr.channel_designation`'s guarded filesystem transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from . import availability, channel_designation
from .config import DATA_DIR, HIDDEN_DIR, TARGET_DIR
from .db import get_db
from .logging_util import log
from .utils import extract_id_from_filename


_ACTIVE_STATUSES = ("queued", "running", "cancelling")
_PLAN_SCHEMA = 2


def _journal_dir() -> Path:
    return DATA_DIR / "channel-designation-journals"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _token(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _entry_path(path: object) -> Path | None:
    if not isinstance(path, str) or not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    return Path(os.path.abspath(candidate))


def _under(path: object, root: Path) -> bool:
    candidate = _entry_path(path)
    if candidate is None:
        return False
    try:
        candidate.relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _rewritten(path: str, old_root: Path, new_root: Path) -> str:
    candidate = _entry_path(path)
    if candidate is None:
        raise availability.AvailabilityError("Stored media path is not absolute", 409)
    try:
        relative = candidate.relative_to(Path(os.path.abspath(old_root)))
    except ValueError as exc:
        raise availability.AvailabilityError(
            "Stored media path escaped the channel folder", 409
        ) from exc
    return str(Path(os.path.abspath(new_root)) / relative)


def _require_identity(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM channel_identities WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        raise availability.AvailabilityError("Channel identity not found", 404)
    return row


def _component(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    stored = row["storage_component"]
    if isinstance(stored, str) and stored:
        return stored
    recovered = conn.execute(
        "SELECT target_component FROM recovered_channel_cases WHERE source_channel_id = ?",
        (row["channel_id"],),
    ).fetchone()
    if recovered is not None:
        return recovered["target_component"]
    return channel_designation.resolve_channel_component(
        row["display_name"] or row["channel_id"],
        provenance=row["provenance"],
    )


def _eligibility(conn: sqlite3.Connection, channel_id: str) -> dict[str, Any]:
    identity = _require_identity(conn, channel_id)
    observations = conn.execute(
        """
        SELECT observation_id, job_id, subject_id, normalized_state,
               reason_code, provider, raw_reason, evidence_json, observed_at
        FROM availability_observations
        WHERE subject_type = 'channel' AND subject_id = ?
        ORDER BY observed_at, observation_id
        """,
        (channel_id,),
    ).fetchall()
    return availability._termination_eligibility_from_rows(
        identity["current_state"],
        identity["current_reason_code"],
        observations,
        requested_channel_id=channel_id,
    )


def _minimum_separation_override_allowed(
    row: sqlite3.Row,
    eligibility: dict[str, Any],
) -> bool:
    """Allow bypassing only time separation after repeated exact evidence."""
    basis = eligibility.get("minimum_separation_override_basis")
    return bool(
        row["designation"] == "none"
        and row["current_state"] == "terminated_explicit"
        and row["current_reason_code"]
        in {"channel_terminated", "channel_suspended"}
        and eligibility.get("matching_reason_code") == row["current_reason_code"]
        and not eligibility.get("eligible")
        and eligibility.get("status")
        in {"waiting_for_interval", "ready_for_followup"}
        and int(eligibility.get("matching_observations") or 0) >= 2
        and eligibility.get("minimum_separation_override_available") is True
        and isinstance(basis, list)
        and len(basis) == 2
    )


def _active_jobs(conn: sqlite3.Connection, channel_id: str) -> list[dict[str, str]]:
    jobs = [
        {"kind": "availability", "job_id": row["job_id"]}
        for row in conn.execute(
            """
            SELECT job_id FROM availability_jobs
            WHERE channel_id = ? AND status IN ('queued', 'running', 'cancelling')
            ORDER BY created_at
            """,
            (channel_id,),
        )
    ]
    jobs.extend(
        {"kind": "recovered_channel", "job_id": row["job_id"]}
        for row in conn.execute(
            """
            SELECT jobs.job_id
            FROM recovered_channel_jobs AS jobs
            JOIN recovered_channel_cases AS cases USING (case_id)
            WHERE cases.source_channel_id = ?
              AND jobs.status IN ('queued', 'running', 'cancelling')
            ORDER BY jobs.created_at
            """,
            (channel_id,),
        )
    )
    return jobs


def _owned_paths(conn: sqlite3.Connection, channel_id: str) -> list[str]:
    video_rows = conn.execute(
        "SELECT video_id, symlink FROM videos WHERE channel_id = ?",
        (channel_id,),
    ).fetchall()
    paths = {
        row["symlink"]
        for row in video_rows
        if isinstance(row["symlink"], str)
    }
    owned_video_ids = {row["video_id"] for row in video_rows}
    owned_video_ids.update({
        row["video_id"]
        for row in conn.execute(
            """
            SELECT items.video_id
            FROM recovered_channel_items AS items
            JOIN recovered_channel_cases AS cases USING (case_id)
            WHERE cases.source_channel_id = ?
            """,
            (channel_id,),
        )
    })
    paths.update(
        row["organized_path"]
        for row in conn.execute(
            """
            SELECT items.organized_path
            FROM recovered_channel_items AS items
            JOIN recovered_channel_cases AS cases USING (case_id)
            WHERE cases.source_channel_id = ? AND items.organized_path IS NOT NULL
            """,
            (channel_id,),
        )
        if isinstance(row["organized_path"], str)
    )
    if owned_video_ids:
        placeholders = ",".join("?" for _ in owned_video_ids)
        paths.update(
            row["filepath"]
            for row in conn.execute(
                f"SELECT filepath FROM lost_media WHERE video_id IN ({placeholders})",
                tuple(sorted(owned_video_ids)),
            )
            if isinstance(row["filepath"], str)
        )
    return sorted(paths)


def _folder_owners(
    conn: sqlite3.Connection,
    root: Path,
    component: str,
    *,
    physical_component: str | None = None,
) -> set[str]:
    """Return exact-ID owners proven by persisted media paths in this folder.

    A registry component or an unpublished recovered case is only a desired
    name; it does not prove that an already-existing physical tree belongs to
    that channel. Ownership therefore comes only from channel-bound video or
    recovered-item paths under this precise managed root.
    """

    def proves_owned_media(
        value: object,
        video_id: object,
        *,
        adopted_real: bool = False,
    ) -> bool:
        stored_path = _entry_path(value)
        if (
            stored_path is None
            or not isinstance(video_id, str)
            or not _under(value, folder)
        ):
            return False
        try:
            relative = stored_path.relative_to(folder)
        except ValueError:
            return False
        path = physical_folder / relative
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            # Stored paths are historical metadata.  They cannot establish
            # ownership of a physical folder unless the claimed entry still
            # exists (broken symlinks intentionally count as existing).
            return False
        if os.path.islink(path):
            try:
                raw_target = Path(os.readlink(path))
            except OSError:
                return False
            entry_id = extract_id_from_filename(path.name)
            target_id = extract_id_from_filename(raw_target.name)
            observed_ids = {candidate for candidate in (entry_id, target_id) if candidate}
            return bool(observed_ids) and observed_ids == {video_id}
        return adopted_real and os.path.isfile(path) and not os.path.islink(path)

    folder = root / component
    physical_folder = root / (physical_component or component)
    owners = {
        row["channel_id"]
        for row in conn.execute(
            """
            SELECT video_id, channel_id, symlink, adopted_real
            FROM videos
            WHERE channel_id IS NOT NULL AND symlink IS NOT NULL
            """
        )
        if proves_owned_media(
            row["symlink"],
            row["video_id"],
            adopted_real=bool(row["adopted_real"]),
        )
    }
    owners.update(
        row["source_channel_id"]
        for row in conn.execute(
            """
            SELECT cases.source_channel_id, items.video_id, items.organized_path
            FROM recovered_channel_items AS items
            JOIN recovered_channel_cases AS cases USING (case_id)
            WHERE items.organized_path IS NOT NULL
            """
        )
        if proves_owned_media(row["organized_path"], row["video_id"])
    )
    return owners


def _destination_owner(
    conn: sqlite3.Connection,
    root: Path,
    component: str,
    *,
    physical_component: str | None = None,
) -> str | None:
    owners = _folder_owners(
        conn,
        root,
        component,
        physical_component=physical_component,
    )
    return next(iter(owners)) if len(owners) == 1 else None


def _folder_locations(component: str) -> list[Path]:
    return [
        root
        for root in (TARGET_DIR, HIDDEN_DIR)
        if os.path.lexists(root / component)
    ]


def _base_blockers(
    row: sqlite3.Row,
    eligibility: dict[str, Any],
    jobs: list[dict[str, str]],
    *,
    override_minimum_separation: bool,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if row["designation"] == "terminated":
        blockers.append({
            "code": "already_designated",
            "message": "This channel is already confirmed and designated as terminated",
        })
    elif row["designation"] != "none":
        blockers.append({
            "code": "designation_conflict",
            "message": "This channel already has another designation",
        })
    if override_minimum_separation and not _minimum_separation_override_allowed(
        row, eligibility
    ):
        blockers.append({
            "code": "minimum_separation_override_unavailable",
            "message": (
                "The 24-hour override requires two distinct, exact matching "
                "termination responses and a currently explicit channel state"
            ),
        })
    elif not override_minimum_separation and not eligibility["eligible"]:
        blockers.append({
            "code": "termination_evidence_incomplete",
            "message": "Two matching explicit termination observations at least 24 hours apart are required",
        })
    if jobs:
        blockers.append({
            "code": "active_jobs",
            "message": "Wait for active availability or recovered-channel jobs",
            "jobs": jobs,
        })
    return blockers


def _journal_blockers() -> list[dict[str, Any]]:
    try:
        unresolved = channel_designation.list_designation_journals(
            journal_dir=_journal_dir()
        )
    except channel_designation.ChannelDesignationError as exc:
        return [{
            "code": "designation_recovery_unavailable",
            "message": f"Designation journal recovery must be resolved first: {exc}",
        }]
    if not unresolved:
        return []
    return [{
        "code": "unresolved_designation_journal",
        "message": "An interrupted channel-folder mutation must be recovered before any new designation",
        "journal_ids": [item.get("journal_id") for item in unresolved],
    }]


def _build_plan_details(
    channel_id: str,
    designation: str = "terminated",
    *,
    override_minimum_separation: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    channel_id = availability.validate_channel_id(channel_id)
    if designation != "terminated":
        raise availability.AvailabilityError("Only terminated designation is supported")
    if not isinstance(override_minimum_separation, bool):
        raise availability.AvailabilityError(
            "override_minimum_separation must be true or false"
        )
    override_actor = str(actor or "").strip()[:120]
    if override_minimum_separation and not override_actor:
        raise availability.AvailabilityError(
            "An authenticated actor is required for the 24-hour override"
        )
    with get_db() as conn:
        row = _require_identity(conn, channel_id)
        source_component = _component(row, conn)
        eligibility = _eligibility(conn, channel_id)
        jobs = _active_jobs(conn, channel_id)
        owned_paths = _owned_paths(conn, channel_id)
        source_locations = _folder_locations(source_component)
        destination_component = channel_designation.component_for_designation(
            source_component, designation
        )
        destination_locations = _folder_locations(destination_component)
        blockers = _base_blockers(
            row,
            eligibility,
            jobs,
            override_minimum_separation=override_minimum_separation,
        )
        blockers.extend(_journal_blockers())
        actual_storage_component = row["storage_component"]

        engine_plan: dict[str, Any] | None = None
        if len(source_locations) > 1:
            blockers.append({
                "code": "source_in_both_roots",
                "message": "The same channel folder exists in both Target and Hidden",
            })
            mode = "blocked"
            root: Path | None = None
        elif len(source_locations) == 1:
            root = source_locations[0]
            source_owner = _destination_owner(conn, root, source_component)
            if source_owner != channel_id:
                blockers.append({
                    "code": "source_identity_ambiguous",
                    "message": "The source folder is not owned exclusively by this exact channel ID",
                })
            opposite_destinations = [
                candidate for candidate in destination_locations if candidate != root
            ]
            if opposite_destinations:
                blockers.append({
                    "code": "destination_in_other_root",
                    "message": "The terminated destination exists in the other visibility root",
                })
            destination_owner = (
                _destination_owner(conn, root, destination_component)
                if root in destination_locations
                else None
            )
            try:
                engine_plan = channel_designation.build_designation_plan(
                    channel_id,
                    source_component,
                    root=root,
                    designation=designation,
                    revision=int(row["revision"]),
                    destination_component=destination_component,
                    destination_channel_id=destination_owner,
                    managed_roots=(TARGET_DIR, HIDDEN_DIR),
                )
            except channel_designation.ChannelDesignationError as exc:
                raise availability.AvailabilityError(str(exc), exc.status) from exc
            blockers.extend(engine_plan["blockers"])
            mode = (
                "metadata_only_existing"
                if engine_plan["mode"] == "noop"
                else engine_plan["mode"]
            )
        else:
            root = None
            if destination_locations:
                blockers.append({
                    "code": "old_folder_missing",
                    "message": "The proposed destination exists but the registered old folder is missing; recover its journal or reconcile ownership",
                })
                mode = "blocked"
            else:
                stale_owned = [
                    path
                    for path in owned_paths
                    if _under(path, TARGET_DIR / source_component)
                    or _under(path, HIDDEN_DIR / source_component)
                ]
                if stale_owned:
                    blockers.append({
                        "code": "owned_paths_without_folder",
                        "message": "Database-owned media paths exist under the missing channel folder",
                        "count": len(stale_owned),
                    })
                mode = "metadata_only"

    plan_payload = {
        "schema": _PLAN_SCHEMA,
        "channel_id": channel_id,
        "designation": designation,
        "revision": int(row["revision"]),
        "actual_storage_component": actual_storage_component,
        "source_component": source_component,
        "destination_component": destination_component,
        "mode": mode,
        "root": str(root) if root is not None else None,
        "eligibility": eligibility,
        "active_jobs": jobs,
        "owned_paths": owned_paths,
        "engine_plan_token": engine_plan["plan_token"] if engine_plan else None,
        "override_minimum_separation": override_minimum_separation,
        "override_actor": override_actor if override_minimum_separation else None,
        "override_basis": (
            eligibility.get("minimum_separation_override_basis", [])
            if override_minimum_separation
            else []
        ),
        "blockers": blockers,
    }
    service_token = _token(plan_payload)
    override_warnings = (
        [
            "Administrator override: the 24-hour evidence separation is bypassed. "
            "Exact-channel evidence, ownership, collision, active-job, dry-run, "
            "plan-token, and path-safety checks still apply."
        ]
        if override_minimum_separation
        else []
    )
    if engine_plan is not None:
        public = {
            **engine_plan,
            "mode": mode,
            "plan_token": service_token,
            "blockers": blockers,
            "safe_to_apply": not blockers,
            "warnings": [
                *(engine_plan.get("warnings") or []),
                *override_warnings,
            ],
        }
    else:
        public = {
            "schema": _PLAN_SCHEMA,
            "channel_id": channel_id,
            "designation": designation,
            "revision": int(row["revision"]),
            "mode": mode,
            "root": str(root) if root is not None else None,
            "source_component": source_component,
            "destination_component": destination_component,
            "source_path": None,
            "destination_path": None,
            "inventory": [],
            "destination_inventory": [],
            "counts": {
                "directories": 0,
                "real_files": 0,
                "symlinks": 0,
                "broken_symlinks": 0,
                "other_entries": 0,
                "real_file_bytes": 0,
            },
            "conflicts": [],
            "warnings": override_warnings,
            "blockers": blockers,
            "safe_to_apply": mode == "metadata_only" and not blockers,
            "source_immutable": True,
            "plan_token": service_token,
        }
    public.update({
        "termination_eligibility": eligibility,
        "confirmation": (
            f"BYPASS 24H AND TERMINATE {channel_id}"
            if override_minimum_separation
            else f"TERMINATE {channel_id}"
        ),
        "minimum_separation_overridden": override_minimum_separation,
        "metadata_only": mode in {"metadata_only", "metadata_only_existing"},
    })
    return {
        "public": public,
        "payload": plan_payload,
        "engine_plan": engine_plan,
    }


def build_designation_plan(
    channel_id: str,
    designation: str = "terminated",
    *,
    override_minimum_separation: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    return _build_plan_details(
        channel_id,
        designation,
        override_minimum_separation=override_minimum_separation,
        actor=actor,
    )["public"]


def _validate_precondition_conn(
    conn: sqlite3.Connection,
    details: dict[str, Any],
    *,
    filesystem_moved: bool = False,
) -> sqlite3.Row:
    payload = details["payload"]
    row = _require_identity(conn, payload["channel_id"])
    if (
        int(row["revision"]) != payload["revision"]
        or row["storage_component"] != payload["actual_storage_component"]
        or row["designation"] != "none"
        or _component(row, conn) != payload["source_component"]
    ):
        raise availability.AvailabilityError(
            "Channel designation state changed; review a new plan", 409
        )
    current_eligibility = _eligibility(conn, payload["channel_id"])
    if payload["override_minimum_separation"]:
        if not _minimum_separation_override_allowed(row, current_eligibility):
            raise availability.AvailabilityError(
                "The 24-hour override evidence is no longer eligible", 409
            )
        if _canonical_json(
            current_eligibility.get("minimum_separation_override_basis", [])
        ) != _canonical_json(payload["override_basis"]):
            raise availability.AvailabilityError(
                "The 24-hour override evidence changed; review a new plan", 409
            )
    elif not current_eligibility["eligible"]:
        raise availability.AvailabilityError(
            "Termination evidence is no longer eligible", 409
        )
    if _active_jobs(conn, payload["channel_id"]):
        raise availability.AvailabilityError(
            "Wait for active channel jobs before designating", 409
        )
    if payload["root"] is not None:
        root = Path(payload["root"])
        source_physical_component = (
            payload["destination_component"]
            if filesystem_moved
            else payload["source_component"]
        )
        if _destination_owner(
            conn,
            root,
            payload["source_component"],
            physical_component=source_physical_component,
        ) != payload["channel_id"]:
            raise availability.AvailabilityError(
                "Source folder ownership changed during designation", 409
            )
        if payload["mode"] == "consolidate" and _destination_owner(
            conn, root, payload["destination_component"]
        ) != payload["channel_id"]:
            raise availability.AvailabilityError(
                "Destination folder ownership changed during designation", 409
            )
    return row


def _rewrite_owned_paths(
    conn: sqlite3.Connection,
    channel_id: str,
    old_root: Path,
    new_root: Path,
) -> None:
    video_rows = conn.execute(
        "SELECT video_id, symlink, status, channel FROM videos WHERE channel_id = ?",
        (channel_id,),
    ).fetchall()
    owned_video_ids = {row["video_id"] for row in video_rows}
    for row in video_rows:
        if not _under(row["symlink"], old_root):
            continue
        new_path = _rewritten(row["symlink"], old_root, new_root)
        cursor = conn.execute(
            """
            UPDATE videos
            SET symlink = ?,
                channel = CASE
                    WHEN status = 'recovered' AND channel = ? THEN ?
                    ELSE channel
                END,
                last_updated = CURRENT_TIMESTAMP
            WHERE video_id = ? AND channel_id = ? AND symlink = ?
            """,
            (
                new_path,
                old_root.name,
                new_root.name,
                row["video_id"],
                channel_id,
                row["symlink"],
            ),
        )
        if cursor.rowcount != 1:
            raise availability.AvailabilityError(
                "Video path ownership changed during designation", 409
            )

    item_rows = conn.execute(
        """
        SELECT items.item_id, items.video_id, items.organized_path
        FROM recovered_channel_items AS items
        JOIN recovered_channel_cases AS cases USING (case_id)
        WHERE cases.source_channel_id = ? AND items.organized_path IS NOT NULL
        """,
        (channel_id,),
    ).fetchall()
    owned_video_ids.update(row["video_id"] for row in item_rows)
    for row in item_rows:
        if not _under(row["organized_path"], old_root):
            continue
        new_path = _rewritten(row["organized_path"], old_root, new_root)
        cursor = conn.execute(
            """
            UPDATE recovered_channel_items
            SET organized_path = ?, updated_at = CURRENT_TIMESTAMP
            WHERE item_id = ? AND organized_path = ?
            """,
            (new_path, row["item_id"], row["organized_path"]),
        )
        if cursor.rowcount != 1:
            raise availability.AvailabilityError(
                "Recovered item path changed during designation", 409
            )

    if owned_video_ids:
        placeholders = ",".join("?" for _ in owned_video_ids)
        lost_rows = conn.execute(
            f"SELECT video_id, filepath FROM lost_media WHERE video_id IN ({placeholders})",
            tuple(sorted(owned_video_ids)),
        ).fetchall()
        for row in lost_rows:
            if not _under(row["filepath"], old_root):
                continue
            new_path = _rewritten(row["filepath"], old_root, new_root)
            cursor = conn.execute(
                "UPDATE lost_media SET filepath = ? WHERE video_id = ? AND filepath = ?",
                (new_path, row["video_id"], row["filepath"]),
            )
            if cursor.rowcount != 1:
                raise availability.AvailabilityError(
                    "Lost-media path changed during designation", 409
                )


def _validate_metadata_only_filesystem(details: dict[str, Any]) -> None:
    """Recheck the absence that authorizes a no-folder designation.

    The initial and locked dry runs are not enough: another writer could create
    either component in the narrow interval before the registry transaction.
    Every in-app writer shares the cross-process library lock, and this final
    check runs while that lock is still held.
    """

    payload = details["payload"]
    if payload["mode"] != "metadata_only":
        return
    for root in (TARGET_DIR, HIDDEN_DIR):
        for component in {
            payload["source_component"],
            payload["destination_component"],
        }:
            if os.path.lexists(root / component):
                raise availability.AvailabilityError(
                    "A channel folder appeared after the dry run; review a new plan",
                    409,
                )


def _commit_designation(
    details: dict[str, Any],
    actor: str,
    *,
    old_root: Path | None,
    new_root: Path | None,
    rename_method: str | None = None,
) -> None:
    payload = details["payload"]
    channel_id = payload["channel_id"]
    try:
        _validate_metadata_only_filesystem(details)
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_precondition_conn(
                conn,
                details,
                filesystem_moved=old_root is not None and new_root is not None,
            )
            if old_root is not None and new_root is not None:
                _rewrite_owned_paths(conn, channel_id, old_root, new_root)
            case = conn.execute(
                "SELECT case_id, target_component FROM recovered_channel_cases WHERE source_channel_id = ?",
                (channel_id,),
            ).fetchone()
            if case is not None and case["target_component"] == payload["source_component"]:
                cursor = conn.execute(
                    """
                    UPDATE recovered_channel_cases
                    SET target_component = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE case_id = ? AND target_component = ?
                    """,
                    (
                        payload["destination_component"],
                        case["case_id"],
                        payload["source_component"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise availability.AvailabilityError(
                        "Recovered case changed during designation", 409
                    )
                conn.execute(
                    """
                    INSERT INTO recovered_channel_events
                        (case_id, actor, event, details_json)
                    VALUES (?, ?, 'channel_designated_terminated', ?)
                    """,
                    (
                        case["case_id"],
                        actor,
                        _canonical_json({
                            "old_component": payload["source_component"],
                            "new_component": payload["destination_component"],
                            "plan_token": details["public"]["plan_token"],
                            "minimum_separation_overridden": payload[
                                "override_minimum_separation"
                            ],
                            "override_basis": payload["override_basis"],
                            "rename_method": rename_method,
                        }),
                    ),
                )
            cursor = conn.execute(
                """
                UPDATE channel_identities
                SET storage_component = ?, designation = 'terminated',
                    designation_confirmed_at = CURRENT_TIMESTAMP,
                    designation_confirmed_by = ?, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND revision = ? AND designation = 'none'
                  AND storage_component IS ?
                """,
                (
                    payload["destination_component"],
                    actor,
                    channel_id,
                    payload["revision"],
                    payload["actual_storage_component"],
                ),
            )
            if cursor.rowcount != 1:
                raise availability.AvailabilityError(
                    "Channel registry changed during designation", 409
                )
            plan_token_hash = hashlib.sha256(
                details["public"]["plan_token"].encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT INTO availability_designation_events
                    (channel_id, actor, event, details_json)
                VALUES (?, ?, 'channel_designated_terminated', ?)
                """,
                (
                    channel_id,
                    actor,
                    _canonical_json({
                        "source_component": payload["source_component"],
                        "destination_component": payload["destination_component"],
                        "mode": payload["mode"],
                        "registry_revision": payload["revision"],
                        "minimum_separation_overridden": payload[
                            "override_minimum_separation"
                        ],
                        "override_basis": payload["override_basis"],
                        "rename_method": rename_method,
                        "plan_token_sha256": plan_token_hash,
                    }),
                ),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        raise availability.AvailabilityError(
            "The terminated channel component conflicts with another owner", 409
        ) from exc


def _database_state_for(
    journal: dict[str, Any],
) -> channel_designation.DatabaseState:
    channel_id = journal["channel_id"]
    old_component = journal["source_component"]
    new_component = journal["destination_component"]
    old_root = Path(journal["root"]) / old_component
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_identities WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if row is None:
            return "unknown"
        resolved_component = _component(row, conn)
        old_paths = [path for path in _owned_paths(conn, channel_id) if _under(path, old_root)]
        case = conn.execute(
            "SELECT target_component FROM recovered_channel_cases WHERE source_channel_id = ?",
            (channel_id,),
        ).fetchone()
    case_component = case["target_component"] if case is not None else None
    if (
        row["storage_component"] == new_component
        and row["designation"] == "terminated"
        and not old_paths
        and case_component in {None, new_component}
    ):
        return "new"
    if (
        resolved_component == old_component
        and row["designation"] == "none"
        and int(row["revision"]) == int(journal["revision"])
        and case_component in {None, old_component}
    ):
        return "old"
    return "mixed"


def apply_designation(
    channel_id: str,
    plan_token: str,
    confirmation: str,
    *,
    actor: str,
    override_minimum_separation: bool = False,
) -> dict[str, Any]:
    channel_id = availability.validate_channel_id(channel_id)
    if not isinstance(override_minimum_separation, bool):
        raise availability.AvailabilityError(
            "override_minimum_separation must be true or false"
        )
    expected_confirmation = (
        f"BYPASS 24H AND TERMINATE {channel_id}"
        if override_minimum_separation
        else f"TERMINATE {channel_id}"
    )
    if confirmation != expected_confirmation:
        raise availability.AvailabilityError(
            f"Type {expected_confirmation} to confirm", 400
        )
    details = _build_plan_details(
        channel_id,
        override_minimum_separation=override_minimum_separation,
        actor=actor,
    )
    if not isinstance(plan_token, str) or not secrets.compare_digest(
        plan_token, details["public"]["plan_token"]
    ):
        raise availability.AvailabilityError(
            "Designation plan changed; review the dry run again", 409
        )
    if details["public"]["blockers"]:
        raise availability.AvailabilityError(
            "Designation plan has safety blockers", 409
        )
    payload = details["payload"]
    if payload["mode"] in {"metadata_only", "metadata_only_existing"}:
        try:
            with channel_designation.designation_mutation_guard(
                journal_dir=_journal_dir()
            ):
                locked_details = _build_plan_details(
                    channel_id,
                    override_minimum_separation=override_minimum_separation,
                    actor=actor,
                )
                if not secrets.compare_digest(
                    plan_token, locked_details["public"]["plan_token"]
                ):
                    raise availability.AvailabilityError(
                        "Designation plan changed; review the dry run again", 409
                    )
                if locked_details["public"]["blockers"]:
                    raise availability.AvailabilityError(
                        "Designation plan has safety blockers", 409
                    )
                _commit_designation(
                    locked_details, actor, old_root=None, new_root=None
                )
                details = locked_details
                payload = details["payload"]
        except channel_designation.ChannelDesignationError as exc:
            raise availability.AvailabilityError(str(exc), exc.status) from exc
        log(
            f"🏷️ [AVAILABILITY] Confirmed future terminated component for {channel_id}: "
            f"{payload['destination_component']}"
            + (
                f" (administrator {actor} bypassed the 24-hour separation)"
                if override_minimum_separation
                else ""
            )
        )
        return {
            "success": True,
            "channel_id": channel_id,
            "designation": "terminated",
            "mode": payload["mode"],
            "old_path": details["public"].get("source_path"),
            "new_path": details["public"].get("destination_path"),
            "storage_component": payload["destination_component"],
            "minimum_separation_overridden": override_minimum_separation,
        }

    engine_plan = details["engine_plan"]
    if engine_plan is None or payload["root"] is None:
        raise availability.AvailabilityError("Designation plan has no safe action", 409)
    root = Path(payload["root"])
    old_root = root / payload["source_component"]
    new_root = root / payload["destination_component"]

    def validate(_plan: dict[str, Any]) -> None:
        with get_db() as conn:
            _validate_precondition_conn(conn, details)

    def update(mutation: dict[str, Any]) -> None:
        rename_method = mutation.get("rename_method")
        _commit_designation(
            details,
            actor,
            old_root=old_root,
            new_root=new_root,
            rename_method=(
                rename_method if isinstance(rename_method, str) else None
            ),
        )

    try:
        result = channel_designation.apply_designation_plan(
            channel_id,
            payload["source_component"],
            engine_plan["plan_token"],
            root=root,
            revision=payload["revision"],
            update_paths=update,
            designation="terminated",
            destination_component=payload["destination_component"],
            destination_channel_id=engine_plan["destination_channel_id"],
            managed_roots=(TARGET_DIR, HIDDEN_DIR),
            journal_dir=_journal_dir(),
            validate_precondition=validate,
            database_state=_database_state_for,
        )
    except channel_designation.ChannelDesignationError as exc:
        raise availability.AvailabilityError(str(exc), exc.status) from exc
    log(
        f"🏷️ [AVAILABILITY] Confirmed terminated channel {channel_id}: "
        f"{result['old_path']} -> {result['new_path']}"
        + (
            " (filesystem compatibility rename)"
            if result.get("rename_method") == "renameat_compat"
            else ""
        )
        + (
            f" (administrator {actor} bypassed the 24-hour separation)"
            if override_minimum_separation
            else ""
        )
    )
    return {
        **result,
        "minimum_separation_overridden": override_minimum_separation,
    }


def recover_pending_designations() -> list[dict[str, Any]]:
    """Complete/roll back interrupted folder mutations before schedulers start."""

    try:
        results = channel_designation.recover_designation_journals(
            _database_state_for,
            managed_roots=(TARGET_DIR, HIDDEN_DIR),
            journal_dir=_journal_dir(),
        )
    except channel_designation.ChannelDesignationError as exc:
        log(f"⛔ [AVAILABILITY] Designation journal recovery failed: {exc}")
        raise
    for journal in results:
        log(
            f"🧾 [AVAILABILITY] Designation journal {journal['journal_id']}: "
            f"{journal['status']}"
        )
    unresolved = channel_designation.list_designation_journals(
        journal_dir=_journal_dir()
    )
    if unresolved:
        identifiers = ", ".join(
            str(item.get("journal_id") or "unknown") for item in unresolved[:3]
        )
        message = (
            "Channel-designation recovery remains unresolved; refusing to start "
            f"library workers ({identifiers})"
        )
        log(f"⛔ [AVAILABILITY] {message}")
        raise channel_designation.ChannelDesignationError(
            message,
            503,
            code="recovery_blocked",
        )
    return results
