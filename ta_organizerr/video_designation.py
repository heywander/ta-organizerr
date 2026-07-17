"""Guarded, reason-specific designation of metadata-backed video folders."""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import stat
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import availability
from .config import HIDDEN_DIR, TARGET_DIR
from . import db as db_module
from .db import get_db
from .library_lock import LIBRARY_MUTATION_LOCK
from .channel_designation import (
    _rename_directory_noreplace_compat,
    resolve_channel_component,
)
from .media_reservations import MediaReservationConflict, media_mutation_guard


_SUFFIXES = {
    "private": ("private", " [Private]"),
    "community_guidelines_removed": (
        "community_guidelines_removed", " [Community Guidelines Removed]"
    ),
    "other_policy_removed": ("other_policy_removed", " [Policy Removed]"),
    "uploader_removed": ("uploader_removed", " [Uploader Removed]"),
    "account_terminated": ("account_terminated", " [Account Terminated]"),
}
_EXPLICIT_PROVIDERS = {"yt-dlp", "yt-dlp+youtube-data-api"}
_REASON_CODES = {
    "private": {"video_private"},
    "community_guidelines_removed": {"community_guidelines"},
    "other_policy_removed": {"copyright", "terms_of_service"},
    "uploader_removed": {"uploader_removed"},
    "account_terminated": {"source_account_terminated"},
}
_PLAN_TTL_SECONDS = 15 * 60


class VideoDesignationError(availability.AvailabilityError):
    pass


def _identity(value: os.stat_result) -> list[int]:
    return [int(value.st_dev), int(value.st_ino)]


def _safe_component(value: str) -> bool:
    return bool(
        value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and len(os.fsencode(value)) <= 255
    )


def _journal_dir() -> Path:
    # Follow the active database root so tests, migrations, and installations
    # with a relocated DB cannot accidentally journal in a different mount.
    directory = db_module.DB_PATH.parent / "video-designation-journals"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise VideoDesignationError("Video designation journal path is unsafe", 503)
    return directory


def _write_journal(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_plan(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or not token:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT payload_json, expires_at
            FROM video_designation_plans
            WHERE token_hash=? AND consumed_at IS NULL
            """,
            (_token_hash(token),),
        ).fetchone()
    if row is None or float(row["expires_at"]) < time.time():
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _unresolved_journals() -> list[Path]:
    result: list[Path] = []
    for path in _journal_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            result.append(path)
            continue
        if data.get("state") not in {"committed", "rolled_back"}:
            result.append(path)
    return result


def _rename_noreplace(operation: dict[str, Any], *, reverse: bool = False) -> None:
    parent = Path(operation["parent"])
    parent_stat = os.lstat(parent)
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise VideoDesignationError("Video folder parent is no longer a real directory", 409)
    descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        source = operation["destination_component"] if reverse else operation["source_component"]
        destination = operation["source_component"] if reverse else operation["destination_component"]
        _rename_directory_noreplace_compat(
            descriptor, source, destination, operation["source_identity"]
        )
    finally:
        os.close(descriptor)


def _recover_journals() -> None:
    """Finish committed journals or reverse filesystem-only mutations."""

    for path in _unresolved_journals():
        try:
            journal = json.loads(path.read_text())
            operations = journal["operations"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        database_states: list[str] = []
        with get_db() as conn:
            for operation in operations:
                row = conn.execute(
                    "SELECT symlink FROM videos WHERE channel_id=? AND video_id=?",
                    (journal.get("channel_id"), operation.get("video_id")),
                ).fetchone()
                old_media = str(Path(operation["source_path"]) / operation["media_name"])
                new_media = str(Path(operation["destination_path"]) / operation["media_name"])
                database_states.append(
                    "new" if row and row["symlink"] == new_media
                    else "old" if row and row["symlink"] == old_media
                    else "unknown"
                )
        if database_states and all(state == "new" for state in database_states):
            valid = True
            for operation in operations:
                destination = Path(operation["destination_path"])
                media = destination / operation["media_name"]
                try:
                    valid = valid and _identity(os.lstat(destination)) == operation["source_identity"]
                    valid = valid and _identity(os.lstat(media)) == operation["media_identity"]
                    if operation.get("raw_link_target") is not None:
                        valid = valid and os.readlink(media) == operation["raw_link_target"]
                except OSError:
                    valid = False
            if valid:
                journal["state"] = "committed"
                journal["recovered_after_restart"] = True
                _write_journal(path, journal)
            continue
        if database_states and all(state == "old" for state in database_states):
            try:
                for operation in reversed(operations):
                    source = Path(operation["source_path"])
                    destination = Path(operation["destination_path"])
                    if os.path.lexists(destination) and not os.path.lexists(source):
                        if _identity(os.lstat(destination)) != operation["source_identity"]:
                            raise VideoDesignationError("Journal destination identity changed", 409)
                        _rename_noreplace(operation, reverse=True)
                journal["state"] = "rolled_back"
                journal["recovered_after_restart"] = True
                _write_journal(path, journal)
            except (OSError, VideoDesignationError):
                continue


@contextmanager
def mutation_guard() -> Iterator[None]:
    with LIBRARY_MUTATION_LOCK:
        _recover_journals()
        unresolved = _unresolved_journals()
        if unresolved:
            raise VideoDesignationError(
                "Resolve the existing video-designation journal before scanning or applying another plan",
                409,
            )
        yield


@contextmanager
def _apply_mutation_guard(plan: dict[str, Any]) -> Iterator[None]:
    try:
        with LIBRARY_MUTATION_LOCK, media_mutation_guard(
            trees=[item["source_path"] for item in plan["operations"]],
            action="rename these video folders",
        ):
            yield
    except MediaReservationConflict as exc:
        raise VideoDesignationError(str(exc), 409) from exc


def _channel_root(row: Any) -> Path | None:
    try:
        component = resolve_channel_component(
            str(row["channel_display_name"] or row["channel_id"]),
            provenance=str(row["channel_provenance"] or "normal"),
            designation=(
                row["channel_designation"]
                if row["channel_designation_confirmed_at"]
                else None
            ),
            persisted_component=row["storage_component"],
        )
    except (ValueError, OSError, RuntimeError):  # registry state fails closed
        return None
    for root in (TARGET_DIR, HIDDEN_DIR):
        candidate = root / component
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    return None


def _operation_from_row(row: Any, action: str) -> dict[str, Any]:
    blockers: list[str] = []
    stored = row["symlink"]
    if row["status"] == "recovered":
        blockers.append("flat_recovered_layout")
    if not isinstance(stored, str) or not stored:
        blockers.append("missing_organized_path")
        return {"video_id": row["video_id"], "blockers": blockers}
    media = Path(os.path.abspath(stored))
    root = _channel_root(row)
    if root is None:
        blockers.append("channel_folder_unavailable")
        return {"video_id": row["video_id"], "path": str(media), "blockers": blockers}
    try:
        relative = media.relative_to(root)
    except ValueError:
        blockers.append("path_outside_owned_channel")
        return {"video_id": row["video_id"], "path": str(media), "blockers": blockers}
    parts = relative.parts
    if len(parts) == 2:
        parent = root
        component = parts[0]
    elif len(parts) == 3 and parts[0] == "#streams":
        parent = root / "#streams"
        component = parts[1]
    else:
        blockers.append("unsupported_nested_layout")
        return {"video_id": row["video_id"], "path": str(media), "blockers": blockers}
    try:
        parent_stat = os.lstat(parent)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or parent.resolve(strict=True) != parent
        ):
            blockers.append("unsafe_video_folder_parent")
    except OSError:
        blockers.append("unsafe_video_folder_parent")
    if not media.name.startswith("video."):
        blockers.append("not_database_owned_video_entry")
    source = parent / component
    try:
        source_stat = os.lstat(source)
        media_stat = os.lstat(media)
    except OSError:
        blockers.append("organized_entry_missing")
        return {"video_id": row["video_id"], "path": str(media), "blockers": blockers}
    if not stat.S_ISDIR(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
        blockers.append("video_parent_not_real_directory")
    if not (stat.S_ISREG(media_stat.st_mode) or stat.S_ISLNK(media_stat.st_mode)):
        blockers.append("video_entry_not_media_or_symlink")
    designation = row["designation"]
    if action == "restore":
        if row["current_state"] != "available" or not designation:
            blockers.append("restoration_requires_available_designated_video")
        destination_component = row["original_component"] or component
        desired_designation = None
    else:
        mapping = _SUFFIXES.get(row["current_state"])
        if mapping is None:
            blockers.append("current_state_not_designatable")
            destination_component = component
            desired_designation = None
        else:
            desired_designation, suffix = mapping
            if row["current_reason_code"] == "copyright":
                desired_designation, suffix = "copyright_removed", " [Copyright Removed]"
            if row["provider"] not in _EXPLICIT_PROVIDERS:
                blockers.append("explicit_yt_dlp_observation_required")
            if row["current_reason_code"] not in _REASON_CODES[row["current_state"]]:
                blockers.append("explicit_reason_code_mismatch")
            base_component = row["original_component"] or component
            destination_component = base_component + suffix
            if component == destination_component and designation == desired_designation:
                blockers.append("designation_already_current")
    if not _safe_component(destination_component):
        blockers.append("unsafe_destination_component")
    if not _safe_component(component):
        blockers.append("unsafe_source_component")
    destination = parent / destination_component
    if destination != source and os.path.lexists(destination):
        blockers.append("destination_exists_no_merge")
    if destination != source:
        folded_destination = unicodedata.normalize("NFC", destination_component).casefold()
        try:
            casefold_collision = any(
                entry.name != destination_component
                and unicodedata.normalize("NFC", entry.name).casefold()
                == folded_destination
                for entry in os.scandir(parent)
            )
        except OSError:
            casefold_collision = True
        if casefold_collision:
            blockers.append("casefold_destination_collision")
    raw_target = os.readlink(media) if media.is_symlink() else None
    return {
        "video_id": row["video_id"],
        "action": action,
        "designation": desired_designation,
        "reason_code": row["current_reason_code"],
        "observation_id": row["last_observation_id"],
        "parent": str(parent),
        "source_component": component,
        "destination_component": destination_component,
        "source_path": str(source),
        "destination_path": str(destination),
        "media_name": media.name,
        "source_identity": _identity(source_stat),
        "media_identity": _identity(media_stat),
        "raw_link_target": raw_target,
        "original_component": row["original_component"] or component,
        "blockers": blockers,
    }


def _load_rows(channel_id: str, video_ids: list[str]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in video_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT videos.video_id, videos.symlink, videos.status,
                   identities.storage_component,
                   identities.display_name AS channel_display_name,
                   identities.provenance AS channel_provenance,
                   identities.designation AS channel_designation,
                   identities.designation_confirmed_at AS channel_designation_confirmed_at,
                   identities.channel_id,
                   current.current_state, current.current_reason_code,
                   current.last_observation_id, observations.provider,
                   designations.designation, designations.original_component,
                   designations.storage_component AS designated_component
            FROM videos
            JOIN channel_identities AS identities
              ON identities.channel_id = videos.channel_id
            LEFT JOIN video_availability AS current
              ON current.channel_id = videos.channel_id
             AND current.video_id = videos.video_id
            LEFT JOIN availability_observations AS observations
              ON observations.observation_id = current.last_observation_id
            LEFT JOIN video_designations AS designations
              ON designations.channel_id = videos.channel_id
             AND designations.video_id = videos.video_id
            WHERE videos.channel_id = ? AND videos.video_id IN ({placeholders})
            """,
            (channel_id, *video_ids),
        ).fetchall()
    return {row["video_id"]: row for row in rows}


def build_plan(
    channel_id: str,
    video_ids: list[str],
    *,
    action: str = "designate",
    actor: str,
) -> dict[str, Any]:
    channel_id = availability.validate_channel_id(channel_id)
    if action not in {"designate", "restore"}:
        raise VideoDesignationError("action must be designate or restore")
    if not isinstance(video_ids, list) or not video_ids or len(video_ids) > 500:
        raise VideoDesignationError("video_ids must contain 1 to 500 exact IDs")
    if len(set(video_ids)) != len(video_ids) or any(
        not isinstance(item, str) or not availability.is_valid_youtube_id(item)
        for item in video_ids
    ):
        raise VideoDesignationError("video_ids must contain unique exact IDs")
    with LIBRARY_MUTATION_LOCK:
        rows = _load_rows(channel_id, video_ids)
        operations = []
        for video_id in video_ids:
            row = rows.get(video_id)
            operations.append(
                _operation_from_row(row, action)
                if row is not None
                else {"video_id": video_id, "blockers": ["video_not_owned_by_channel"]}
            )
    blockers = [
        {"video_id": item["video_id"], "code": blocker}
        for item in operations for blocker in item.get("blockers", [])
    ]
    token = secrets.token_urlsafe(32)
    confirmation = f"APPLY {len(video_ids)} VIDEO FOLDERS"
    private = {
        "channel_id": channel_id,
        "video_ids": list(video_ids),
        "action": action,
        "actor": actor,
        "operations": operations,
        "expires_at": time.time() + _PLAN_TTL_SECONDS,
        "confirmation": confirmation,
    }
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO video_designation_plans
                (token_hash, channel_id, actor, payload_json, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _token_hash(token), channel_id, actor,
                json.dumps(private, sort_keys=True, separators=(",", ":")),
                private["expires_at"],
            ),
        )
        conn.execute(
            "DELETE FROM video_designation_plans WHERE expires_at < ?",
            (time.time() - 86400,),
        )
        conn.commit()
    return {
        "plan_token": token,
        "channel_id": channel_id,
        "action": action,
        "operations": operations,
        "blockers": blockers,
        "can_apply": not blockers,
        "confirmation": confirmation,
        "expires_at": private["expires_at"],
    }


def apply_plan(
    channel_id: str,
    plan_token: str,
    confirmation: str,
    *,
    actor: str,
) -> dict[str, Any]:
    channel_id = availability.validate_channel_id(channel_id)
    plan = _load_plan(plan_token)
    if not plan:
        raise VideoDesignationError("Plan expired; review a new dry run", 409)
    if plan["channel_id"] != channel_id or plan["actor"] != actor:
        raise VideoDesignationError("Plan belongs to a different channel or actor", 403)
    if not secrets.compare_digest(confirmation, plan["confirmation"]):
        raise VideoDesignationError(f"Type {plan['confirmation']} to confirm")
    if any(item.get("blockers") for item in plan["operations"]):
        raise VideoDesignationError("Blocked plan cannot be applied", 409)

    journal = {
        "schema": 1,
        "journal_id": secrets.token_hex(12),
        "state": "prepared",
        "channel_id": channel_id,
        "actor": actor,
        "action": plan["action"],
        "operations": plan["operations"],
        "completed": [],
    }
    journal_path = _journal_dir() / f"{journal['journal_id']}.json"
    moved: list[dict[str, Any]] = []
    database_committed = False
    with _apply_mutation_guard(plan):
        locked_plan = _load_plan(plan_token)
        if locked_plan != plan:
            raise VideoDesignationError("Plan was consumed or changed; review a new dry run", 409)
        if _unresolved_journals():
            raise VideoDesignationError("Another video designation needs recovery", 409)
        current_rows = _load_rows(channel_id, plan["video_ids"])
        if set(current_rows) != set(plan["video_ids"]):
            raise VideoDesignationError("Video ownership changed; review a new plan", 409)
        refreshed = [
            _operation_from_row(current_rows[item], plan["action"])
            for item in plan["video_ids"]
        ]
        for old, new in zip(plan["operations"], refreshed):
            stable_fields = (
                "source_path", "destination_path", "source_identity",
                "media_identity", "raw_link_target", "observation_id",
                "designation",
            )
            if new.get("blockers") or any(old.get(key) != new.get(key) for key in stable_fields):
                raise VideoDesignationError("Video state or filesystem identity changed; review a new plan", 409)
        _write_journal(journal_path, journal)
        try:
            for operation in refreshed:
                destination = Path(operation["destination_path"])
                if os.path.lexists(destination):
                    raise VideoDesignationError("Destination appeared after the dry run", 409)
                _rename_noreplace(operation)
                moved.append(operation)
                journal["completed"].append(operation["video_id"])
                journal["state"] = "filesystem_moving"
                _write_journal(journal_path, journal)

            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for operation in refreshed:
                    old_media = str(Path(operation["source_path"]) / operation["media_name"])
                    new_media = str(Path(operation["destination_path"]) / operation["media_name"])
                    cursor = conn.execute(
                        """
                        UPDATE videos SET symlink=?, last_updated=CURRENT_TIMESTAMP
                        WHERE channel_id=? AND video_id=? AND symlink=?
                        """,
                        (new_media, channel_id, operation["video_id"], old_media),
                    )
                    if cursor.rowcount != 1:
                        raise VideoDesignationError("Database path ownership changed during apply", 409)
                    conn.execute(
                        "UPDATE lost_media SET filepath=? WHERE video_id=? AND filepath=?",
                        (new_media, operation["video_id"], old_media),
                    )
                    if plan["action"] == "restore":
                        conn.execute(
                            "DELETE FROM video_designations WHERE channel_id=? AND video_id=?",
                            (channel_id, operation["video_id"]),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO video_designations
                                (channel_id, video_id, designation, reason_code,
                                 original_component, storage_component, observation_id,
                                 confirmed_at, confirmed_by)
                            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                            ON CONFLICT(channel_id, video_id) DO UPDATE SET
                                designation=excluded.designation,
                                reason_code=excluded.reason_code,
                                storage_component=excluded.storage_component,
                                observation_id=excluded.observation_id,
                                confirmed_at=CURRENT_TIMESTAMP,
                                confirmed_by=excluded.confirmed_by,
                                revision=video_designations.revision+1,
                                updated_at=CURRENT_TIMESTAMP
                            """,
                            (
                                channel_id, operation["video_id"], operation["designation"],
                                operation["reason_code"], operation["original_component"],
                                operation["destination_component"], operation["observation_id"], actor,
                            ),
                        )
                    conn.execute(
                        """
                        INSERT INTO video_designation_events
                            (channel_id, video_id, actor, event, details_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            channel_id, operation["video_id"], actor,
                            "restored" if plan["action"] == "restore" else "designated",
                            json.dumps({"from": old_media, "to": new_media,
                                        "reason_code": operation["reason_code"]}, sort_keys=True),
                        ),
                    )
                consumed = conn.execute(
                    """
                    UPDATE video_designation_plans
                    SET consumed_at=CURRENT_TIMESTAMP
                    WHERE token_hash=? AND consumed_at IS NULL
                    """,
                    (_token_hash(plan_token),),
                )
                if consumed.rowcount != 1:
                    raise VideoDesignationError("Plan was already consumed", 409)
                conn.commit()
                database_committed = True
            journal["state"] = "committed"
            _write_journal(journal_path, journal)
        except Exception as exc:
            if database_committed:
                # The database is authoritative after commit. Leave the
                # filesystem in its new state; the next mutation guard will
                # validate identities and finalize the durable journal.
                raise VideoDesignationError(
                    "Folder changes committed, but journal finalization needs recovery",
                    503,
                ) from exc
            rollback_error = None
            for operation in reversed(moved):
                try:
                    destination = Path(operation["destination_path"])
                    source = Path(operation["source_path"])
                    if destination.exists() and not os.path.lexists(source):
                        _rename_noreplace(operation, reverse=True)
                except (OSError, VideoDesignationError) as rollback_exc:
                    rollback_error = rollback_exc
            journal["state"] = "rollback_failed" if rollback_error else "rolled_back"
            journal["error"] = str(exc)[:2000]
            journal["rollback_error"] = str(rollback_error)[:2000] if rollback_error else None
            _write_journal(journal_path, journal)
            if rollback_error:
                raise VideoDesignationError(
                    "Apply failed and rollback needs operator recovery; media was not deleted", 503
                ) from exc
            raise
    return {
        "status": "committed",
        "action": plan["action"],
        "count": len(moved),
        "journal_id": journal["journal_id"],
    }
