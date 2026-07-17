"""Guarded channel-folder designation and recovery.

This module deliberately has no database dependency.  The availability
service owns channel identity and evidence; this module owns the narrow
filesystem boundary used after an administrator confirms a designation.

The caller supplies two database hooks when applying a plan:

``validate_precondition(plan)``
    Recheck the registry revision, channel ownership, confirmation state, and
    absence of active jobs.  It runs under the shared library writer lock and
    immediately before the journal is created.

``update_paths(mutation)``
    In one database transaction, compare-and-swap the registry revision and
    old storage component, then rewrite every owned path prefix (including
    recovered-case components).  It must either commit everything and return,
    or roll back everything and raise.

Media bytes and symlink text are never rewritten.  A destination that already
exists is consolidated only when its exact channel ID is supplied and matches;
even then, any relative-path collision blocks the complete operation.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal

from .config import DATA_DIR, HIDDEN_DIR, TARGET_DIR
from .library_lock import LIBRARY_MUTATION_LOCK, LibraryMutationLockError
from .media_reservations import MediaReservationConflict, media_mutation_guard
from .utils import channel_component


_CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")
_DESIGNATIONS = {"terminated"}
_TERMINATED_SUFFIX = " [Terminated]"
_RECOVERED_SUFFIX = " [Recovered]"
_JOURNAL_SCHEMA = 1
_PLAN_SCHEMA = 1
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TERMINAL_JOURNAL_STATES = {"committed", "rolled_back"}


class ChannelDesignationError(RuntimeError):
    """Operator-facing designation failure with an HTTP-compatible status."""

    def __init__(self, message: str, status: int = 400, *, code: str = "invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


PreconditionCallback = Callable[[dict[str, Any]], None]
PathUpdateCallback = Callable[[dict[str, Any]], None]
DatabaseState = Literal["old", "new", "mixed", "unknown"]
DatabaseStateCallback = Callable[[dict[str, Any]], DatabaseState]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_channel_id(value: object) -> str:
    if not isinstance(value, str) or _CHANNEL_ID_RE.fullmatch(value) is None:
        raise ChannelDesignationError(
            "channel_id must be an exact 24-character UC channel ID"
        )
    return value


def _validated_designation(value: object) -> str:
    if value not in _DESIGNATIONS:
        raise ChannelDesignationError("Only the terminated designation is supported")
    return str(value)


def _validated_component(value: object, field: str = "component") -> str:
    if not isinstance(value, str):
        raise ChannelDesignationError(f"{field} must be a string")
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or unicodedata.normalize("NFC", value) != value
        or len(os.fsencode(value)) > 255
    ):
        raise ChannelDesignationError(f"{field} is not a safe folder component")
    return value


def component_for_designation(
    component: str,
    designation: str | None,
) -> str:
    """Return the persisted storage component for a confirmed designation.

    The function is intentionally idempotent and operates on the already
    persisted component.  A recovered component therefore becomes
    ``Name [Recovered] [Terminated]`` without losing its provenance marker.
    """

    component = _validated_component(component)
    if designation in (None, "", "none"):
        return component
    designation = _validated_designation(designation)
    if designation == "terminated" and not component.casefold().endswith(
        _TERMINATED_SUFFIX.casefold()
    ):
        component = f"{component}{_TERMINATED_SUFFIX}"
    return _validated_component(component, "designated component")


def resolve_channel_component(
    display_name: str,
    *,
    provenance: str = "normal",
    designation: str | None = None,
    persisted_component: str | None = None,
) -> str:
    """Central component resolver for scans, recovery, and hidden moves.

    Callers should pass the registry's ``persisted_component`` whenever one is
    present.  The designation is honored only when the caller supplies the
    registry's confirmed ``designation`` value; online probe state alone must
    never be passed here as a designation.
    """

    if persisted_component is not None:
        base = _validated_component(persisted_component, "persisted_component")
    else:
        base = channel_component(display_name)
        if provenance == "recovered" and not base.casefold().endswith(
            _RECOVERED_SUFFIX.casefold()
        ):
            base = f"{base}{_RECOVERED_SUFFIX}"
        elif provenance not in {"normal", "source", "recovered"}:
            raise ChannelDesignationError("Unknown channel provenance")
    return component_for_designation(base, designation)


def _identity(file_stat: os.stat_result) -> list[int]:
    return [file_stat.st_dev, file_stat.st_ino]


def _same_identity(file_stat: os.stat_result, expected: Iterable[int]) -> bool:
    values = tuple(expected)
    return len(values) == 2 and (file_stat.st_dev, file_stat.st_ino) == values


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "real_file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _entry_snapshot(path: Path, relative: Path) -> dict[str, Any]:
    try:
        entry_stat = os.lstat(path)
    except OSError as exc:
        raise ChannelDesignationError(
            f"Channel inventory changed while it was being read: {path}: {exc}",
            409,
            code="inventory_changed",
        ) from exc
    item: dict[str, Any] = {
        "path": relative.as_posix(),
        "kind": _kind(entry_stat.st_mode),
        "device": entry_stat.st_dev,
        "inode": entry_stat.st_ino,
        "mode": stat.S_IMODE(entry_stat.st_mode),
        "size_bytes": entry_stat.st_size,
        "mtime_ns": entry_stat.st_mtime_ns,
        "ctime_ns": entry_stat.st_ctime_ns,
    }
    if item["kind"] == "symlink":
        try:
            item["target"] = os.readlink(path)
            os.stat(path)
            item["link_state"] = "resolves"
        except FileNotFoundError:
            item["link_state"] = "broken"
        except OSError as exc:
            item["link_state"] = "unreadable"
            item["link_error"] = str(exc)
    return item


def _path_is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _lexical_target(link: Path, raw_target: str) -> Path:
    target = Path(raw_target)
    if not target.is_absolute():
        target = link.parent / target
    return Path(os.path.normpath(os.fspath(target)))


def _resolved_target(link: Path, raw_target: str) -> Path | None:
    """Resolve existing ancestor/link chains without requiring the final file."""

    try:
        return _lexical_target(link, raw_target).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _mapped_link_target(
    link: Path,
    raw_target: str,
    source: Path,
    destination: Path,
) -> tuple[Path, Path]:
    old_target = _lexical_target(link, raw_target)
    new_link = destination / link.relative_to(source)
    new_target = _lexical_target(new_link, raw_target)
    expected = (
        destination / old_target.relative_to(source)
        if _path_is_relative_to(old_target, source)
        else old_target
    )
    return new_target, expected


def _scan_tree(
    source: Path,
    destination: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    inventory: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    counts = {
        "directories": 0,
        "real_files": 0,
        "symlinks": 0,
        "broken_symlinks": 0,
        "other_entries": 0,
        "real_file_bytes": 0,
    }

    def visit(directory: Path, relative_directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ChannelDesignationError(
                f"Could not inventory channel folder {directory}: {exc}",
                409,
                code="inventory_unreadable",
            ) from exc
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            item = _entry_snapshot(path, relative)
            inventory.append(item)
            kind = item["kind"]
            if kind == "directory":
                counts["directories"] += 1
                visit(path, relative)
            elif kind == "real_file":
                counts["real_files"] += 1
                counts["real_file_bytes"] += item["size_bytes"]
            elif kind == "symlink":
                counts["symlinks"] += 1
                if item.get("link_state") == "broken":
                    counts["broken_symlinks"] += 1
                raw_target = item.get("target")
                if isinstance(raw_target, str):
                    new_target, expected = _mapped_link_target(
                        path, raw_target, source, destination
                    )
                    if new_target != expected:
                        blockers.append({
                            "code": "symlink_meaning_changes",
                            "path": str(path),
                            "target": raw_target,
                            "message": (
                                "This symlink would point somewhere different after the "
                                "channel folder is renamed"
                            ),
                        })
                    lexical_target = _lexical_target(path, raw_target)
                    resolved_target = _resolved_target(path, raw_target)
                    if resolved_target is None:
                        blockers.append({
                            "code": "ambiguous_symlink_chain",
                            "path": str(path),
                            "target": raw_target,
                            "message": "This symlink's ancestor chain could not be resolved safely",
                        })
                    elif (
                        _path_is_relative_to(resolved_target, source)
                        and not _path_is_relative_to(lexical_target, source)
                    ):
                        blockers.append({
                            "code": "indirect_internal_symlink",
                            "path": str(path),
                            "target": raw_target,
                            "message": (
                                "This symlink reaches the channel through another symlink; "
                                "its meaning cannot be preserved by renaming only the channel"
                            ),
                        })
            else:
                counts["other_entries"] += 1
                blockers.append({
                    "code": "unsupported_entry_type",
                    "path": str(path),
                    "message": "Socket, device, FIFO, and other special entries are not moved",
                })

    visit(source, Path())
    inventory.sort(key=lambda item: item["path"])
    return inventory, blockers, counts


def _scan_inbound_links(
    source: Path,
    managed_roots: tuple[Path, ...],
) -> list[dict[str, Any]]:
    inbound: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        if directory == source:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ChannelDesignationError(
                f"Could not inspect managed folder for inbound links: {directory}: {exc}",
                409,
                code="inventory_unreadable",
            ) from exc
        for entry in entries:
            path = directory / entry.name
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ChannelDesignationError(
                    f"Managed inventory changed during inbound-link scan: {path}: {exc}",
                    409,
                    code="inventory_changed",
                ) from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(path)
                continue
            if not stat.S_ISLNK(entry_stat.st_mode):
                continue
            try:
                raw_target = os.readlink(path)
            except OSError as exc:
                raise ChannelDesignationError(
                    f"Could not read managed symlink {path}: {exc}",
                    409,
                    code="inventory_unreadable",
                ) from exc
            target = _lexical_target(path, raw_target)
            resolved_target = _resolved_target(path, raw_target)
            if resolved_target is None:
                inbound.append({
                    "code": "ambiguous_inbound_symlink",
                    "path": str(path),
                    "target": raw_target,
                    "message": "A managed symlink chain could not be resolved safely",
                })
            elif _path_is_relative_to(target, source) or _path_is_relative_to(
                resolved_target, source
            ):
                inbound.append({
                    "code": "inbound_symlink",
                    "path": str(path),
                    "target": raw_target,
                    "message": "A symlink outside the channel folder points into it",
                })

    for root in managed_roots:
        visit(root)
    return inbound


def _merge_layout(
    source_inventory: list[dict[str, Any]],
    destination_inventory: list[dict[str, Any]],
    source_stat: os.stat_result,
    destination_stat: os.stat_result,
    destination: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Plan recursive, conflict-free consolidation.

    Matching real directories are containers, not conflicts.  Their children
    are compared recursively.  Every non-directory destination collision is
    a blocker, even when size, inode, or symlink text happens to match.
    """

    source_by_path = {item["path"]: item for item in source_inventory}
    destination_by_path = {item["path"]: item for item in destination_inventory}
    destination_by_folded_path = {
        _folded_relative_path(item["path"]): item
        for item in destination_inventory
    }
    operations: list[dict[str, Any]] = []
    merged_directories: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    def parent_item(
        inventory: dict[str, dict[str, Any]],
        parent: str,
        root_entry: os.stat_result,
    ) -> tuple[list[int], int]:
        if not parent:
            return _identity(root_entry), stat.S_IMODE(root_entry.st_mode)
        item = inventory[parent]
        return [item["device"], item["inode"]], item["mode"]

    # A child of a source directory that does not exist at the destination can
    # move as one entry, including its complete subtree.  Descendants of that
    # child must not become separate operations.
    covered_prefixes: list[str] = []
    for relative_path in sorted(
        source_by_path,
        key=lambda value: (len(Path(value).parts), value),
    ):
        if any(
            relative_path == prefix or relative_path.startswith(prefix + "/")
            for prefix in covered_prefixes
        ):
            continue
        source_item = source_by_path[relative_path]
        destination_item = destination_by_path.get(relative_path)
        folded_destination_item = destination_by_folded_path.get(
            _folded_relative_path(relative_path)
        )
        if destination_item is None and folded_destination_item is not None:
            conflicts.append({
                "code": "casefold_collision",
                "path": str(destination / folded_destination_item["path"]),
                "relative_path": relative_path,
                "destination_relative_path": folded_destination_item["path"],
                "message": (
                    "Source and destination paths differ only by Unicode normalization or case"
                ),
            })
            continue
        parent = Path(relative_path).parent.as_posix()
        parent = "" if parent == "." else parent
        if destination_item is None:
            source_parent_identity, _ = parent_item(
                source_by_path, parent, source_stat
            )
            destination_parent_identity, _ = parent_item(
                destination_by_path, parent, destination_stat
            )
            operations.append({
                "relative_path": relative_path,
                "kind": source_item["kind"],
                "identity": [source_item["device"], source_item["inode"]],
                "source_parent_identity": source_parent_identity,
                "destination_parent_identity": destination_parent_identity,
            })
            if source_item["kind"] == "directory":
                covered_prefixes.append(relative_path)
            continue
        if source_item["kind"] == destination_item["kind"] == "directory":
            merged_directories.append({
                "relative_path": relative_path,
                "source_identity": [source_item["device"], source_item["inode"]],
                "destination_identity": [
                    destination_item["device"], destination_item["inode"]
                ],
                "mode": source_item["mode"],
            })
            continue
        conflicts.append({
            "code": "path_collision",
            "path": str(destination / relative_path),
            "relative_path": relative_path,
            "source_kind": source_item["kind"],
            "destination_kind": destination_item["kind"],
            "message": "The destination entry already exists and will not be overwritten",
        })
    return operations, merged_directories, conflicts


def _folded_relative_path(relative_path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold()
        for part in Path(relative_path).parts
    )


def _inventory_name_collisions(
    inventory: list[dict[str, Any]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    seen: dict[str, str] = {}
    collisions: list[dict[str, Any]] = []
    for item in inventory:
        relative = item["path"]
        folded = _folded_relative_path(relative)
        prior = seen.get(folded)
        if prior is not None and prior != relative:
            collisions.append({
                "code": "casefold_collision",
                "path": relative,
                "other_path": prior,
                "message": (
                    f"{label} contains paths that differ only by Unicode normalization or case"
                ),
            })
        else:
            seen[folded] = relative
    return collisions


def _normalize_managed_roots(
    managed_roots: Iterable[Path] | None,
) -> tuple[Path, ...]:
    roots = tuple(Path(root) for root in (managed_roots or (TARGET_DIR, HIDDEN_DIR)))
    if not roots:
        raise ChannelDesignationError("At least one managed root is required")
    normalized: list[Path] = []
    for root in roots:
        if not root.is_absolute():
            raise ChannelDesignationError("Managed roots must be absolute paths")
        lexical = Path(os.path.abspath(root))
        try:
            root_stat = os.lstat(lexical)
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise ChannelDesignationError(
                f"Managed root is unavailable: {lexical}: {exc}", 503,
                code="root_unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or resolved != lexical
        ):
            raise ChannelDesignationError(
                f"Managed root must be a real, canonical directory: {lexical}",
                409,
                code="unsafe_root",
            )
        normalized.append(lexical)
    return tuple(normalized)


@contextmanager
def _open_root(root: Path, managed_roots: tuple[Path, ...]) -> Iterator[tuple[Path, int, os.stat_result]]:
    root = Path(os.path.abspath(root))
    if root not in managed_roots:
        raise ChannelDesignationError(
            "Channel folder must be directly inside Target or Hidden",
            400,
            code="outside_managed_root",
        )
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        descriptor_stat = os.fstat(root_fd)
        path_stat = os.lstat(root)
    except OSError as exc:
        raise ChannelDesignationError(
            f"Could not pin managed root {root}: {exc}", 503,
            code="root_unavailable",
        ) from exc
    try:
        if not _same_identity(path_stat, _identity(descriptor_stat)):
            raise ChannelDesignationError(
                "Managed root changed while it was being opened",
                409,
                code="inventory_changed",
            )
        yield root, root_fd, descriptor_stat
    finally:
        os.close(root_fd)


def _child_stat(root_fd: int, component: str) -> os.stat_result | None:
    try:
        return os.stat(component, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _directory_child_stat(root_fd: int, component: str, field: str) -> os.stat_result:
    result = _child_stat(root_fd, component)
    if result is None:
        raise ChannelDesignationError(
            f"{field} does not exist", 404, code="folder_missing"
        )
    if not stat.S_ISDIR(result.st_mode) or stat.S_ISLNK(result.st_mode):
        raise ChannelDesignationError(
            f"{field} must be a real directory, not a symlink",
            409,
            code="unsafe_folder",
        )
    return result


def _casefold_collision(root: Path, destination_component: str) -> str | None:
    folded = unicodedata.normalize("NFC", destination_component).casefold()
    try:
        names = [entry.name for entry in os.scandir(root)]
    except OSError as exc:
        raise ChannelDesignationError(
            f"Could not inspect managed root names: {exc}", 503,
            code="root_unavailable",
        ) from exc
    for name in names:
        if name != destination_component and unicodedata.normalize("NFC", name).casefold() == folded:
            return name
    return None


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _plan_token(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_designation_plan(
    channel_id: str,
    source_component: str,
    *,
    root: Path,
    designation: str = "terminated",
    revision: int | str,
    destination_component: str | None = None,
    destination_channel_id: str | None = None,
    managed_roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Build a revision-bound, complete dry run for a folder designation."""

    channel_id = _validated_channel_id(channel_id)
    designation = _validated_designation(designation)
    source_component = _validated_component(source_component, "source_component")
    destination_component = _validated_component(
        destination_component or component_for_designation(source_component, designation),
        "destination_component",
    )
    if destination_channel_id is not None:
        destination_channel_id = _validated_channel_id(destination_channel_id)
    roots = _normalize_managed_roots(managed_roots)

    with LIBRARY_MUTATION_LOCK, _open_root(Path(root), roots) as (
        canonical_root,
        root_fd,
        root_stat,
    ):
        source_stat = _directory_child_stat(root_fd, source_component, "Source channel folder")
        source = canonical_root / source_component
        destination = canonical_root / destination_component
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        if source_component == destination_component:
            mode = "noop"
            destination_stat = source_stat
            destination_inventory: list[dict[str, Any]] = []
        else:
            destination_stat = _child_stat(root_fd, destination_component)
            if destination_stat is None:
                mode = "rename"
                destination_inventory = []
                collision = _casefold_collision(canonical_root, destination_component)
                if collision is not None:
                    blockers.append({
                        "code": "casefold_collision",
                        "path": str(canonical_root / collision),
                        "message": "A case-insensitive variant of the destination already exists",
                    })
            elif not stat.S_ISDIR(destination_stat.st_mode) or stat.S_ISLNK(
                destination_stat.st_mode
            ):
                mode = "blocked"
                destination_inventory = []
                blockers.append({
                    "code": "destination_not_directory",
                    "path": str(destination),
                    "message": "The destination is not a real directory",
                })
            else:
                mode = "consolidate"
                destination_inventory, destination_link_blockers, _ = _scan_tree(
                    destination, destination
                )
                # Scanning an existing destination against itself should not
                # yield link-mapping blockers, but special entries still matter.
                blockers.extend(destination_link_blockers)
                if destination_channel_id != channel_id:
                    blockers.append({
                        "code": (
                            "destination_identity_unknown"
                            if destination_channel_id is None
                            else "destination_identity_mismatch"
                        ),
                        "path": str(destination),
                        "message": (
                            "The existing destination must be proven to belong to the same exact channel ID"
                        ),
                    })

        inventory, tree_blockers, counts = _scan_tree(source, destination)
        blockers.extend(tree_blockers)
        blockers.extend(
            _inventory_name_collisions(inventory, label="Source channel folder")
        )
        blockers.extend(
            _inventory_name_collisions(
                destination_inventory, label="Destination channel folder"
            )
        )
        inbound = _scan_inbound_links(source, roots)
        blockers.extend(inbound)

        source_top = {
            item["path"]: item for item in inventory if "/" not in item["path"]
        }
        conflicts: list[dict[str, Any]] = []
        merged_directories: list[dict[str, Any]] = []
        if mode == "consolidate":
            move_items, merged_directories, conflicts = _merge_layout(
                inventory,
                destination_inventory,
                source_stat,
                destination_stat,
                destination,
            )
            blockers.extend(conflicts)
        else:
            move_items = [
                {
                    "relative_path": name,
                    "kind": source_top[name]["kind"],
                    "identity": [
                        source_top[name]["device"], source_top[name]["inode"]
                    ],
                    "source_parent_identity": _identity(source_stat),
                    "destination_parent_identity": None,
                }
                for name in sorted(source_top)
            ]

        if counts["broken_symlinks"]:
            warnings.append({
                "code": "broken_symlinks_preserved",
                "message": "Broken symlinks will be preserved exactly as they are",
                "count": counts["broken_symlinks"],
            })

        payload = {
            "schema": _PLAN_SCHEMA,
            "channel_id": channel_id,
            "designation": designation,
            "revision": revision,
            "root": str(canonical_root),
            "root_identity": _identity(root_stat),
            "source_component": source_component,
            "destination_component": destination_component,
            "destination_channel_id": destination_channel_id,
            "source_identity": _identity(source_stat),
            "source_mode": stat.S_IMODE(source_stat.st_mode),
            "destination_identity": (
                _identity(destination_stat)
                if destination_stat is not None and source_component != destination_component
                else None
            ),
            "mode": mode,
            "inventory": inventory,
            "destination_inventory": destination_inventory,
            "move_items": move_items,
            "merged_directories": merged_directories,
            "blockers": blockers,
            "warnings": warnings,
            "conflicts": conflicts,
            "counts": counts,
        }
        token = _plan_token(payload)
        return {
            **payload,
            "plan_token": token,
            "source_path": str(source),
            "destination_path": str(destination),
            "safe_to_apply": mode != "noop" and not blockers,
            "source_immutable": True,
        }


def _rename_noreplace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ChannelDesignationError(
            "Safe no-overwrite rename is unavailable on this system",
            503,
            code="rename_noreplace_unavailable",
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), source_name)


def _ensure_journal_dir(journal_dir: Path | None) -> Path:
    directory = Path(journal_dir or (DATA_DIR / "channel-designation-journals"))
    if not directory.is_absolute():
        raise ChannelDesignationError("Journal directory must be absolute", 503)
    parent = directory.parent
    try:
        parent_stat = os.lstat(parent)
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise ChannelDesignationError("Journal parent must be a real directory", 503)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory_stat = os.lstat(directory)
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
            raise ChannelDesignationError("Journal path must be a real directory", 503)
        directory.chmod(0o700)
    except ChannelDesignationError:
        raise
    except OSError as exc:
        raise ChannelDesignationError(
            f"Could not prepare designation journal: {exc}", 503,
            code="journal_unavailable",
        ) from exc
    return directory


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, _DIRECTORY_FLAGS)
    try:
        _fsync_fd(descriptor)
    finally:
        os.close(descriptor)


def _fsync_fd(descriptor: int) -> None:
    """Durably order directory-entry mutations before journal/DB progress."""

    os.fsync(descriptor)


def _write_journal(directory: Path, journal: dict[str, Any]) -> Path:
    journal_id = journal["journal_id"]
    destination = directory / f"{journal_id}.json"
    temporary = directory / f".{journal_id}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        payload = (_canonical_json(journal) + "\n").encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        _fsync_directory(directory)
        return destination
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ChannelDesignationError(
            f"Could not persist designation journal: {exc}", 503,
            code="journal_unavailable",
        ) from exc


def _load_journal(path: Path) -> dict[str, Any]:
    try:
        path_stat = os.lstat(path)
        if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
            raise ChannelDesignationError("Designation journal is not a real file", 503)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ChannelDesignationError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ChannelDesignationError(
            f"Could not read designation journal {path.name}: {exc}", 503,
            code="journal_unreadable",
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != _JOURNAL_SCHEMA:
        raise ChannelDesignationError("Unknown designation journal schema", 503)
    return payload


@contextmanager
def _cross_process_lock(journal_dir: Path) -> Iterator[None]:
    lock_path = journal_dir / ".mutation.lock"
    try:
        with LIBRARY_MUTATION_LOCK.scoped(lock_path, blocking=False):
            yield
    except LibraryMutationLockError as exc:
        raise ChannelDesignationError(
            str(exc),
            409 if exc.busy else 503,
            code="mutation_busy" if exc.busy else "lock_unavailable",
        ) from exc


def _journal_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": _JOURNAL_SCHEMA,
        "journal_id": secrets.token_hex(16),
        "status": "planned",
        "created_at": _now(),
        "updated_at": _now(),
        "channel_id": plan["channel_id"],
        "designation": plan["designation"],
        "revision": plan["revision"],
        "plan_token": plan["plan_token"],
        "root": plan["root"],
        "root_identity": plan["root_identity"],
        "source_component": plan["source_component"],
        "destination_component": plan["destination_component"],
        "source_identity": plan["source_identity"],
        "source_mode": plan["source_mode"],
        "destination_identity": plan["destination_identity"],
        "mode": plan["mode"],
        "plan_snapshot": {
            "inventory": plan["inventory"],
            "destination_inventory": plan["destination_inventory"],
        },
        "operations": [
            {**operation, "state": "pending"}
            for operation in plan["move_items"]
        ],
        "merged_directories": [
            {**directory, "state": "present"}
            for directory in plan["merged_directories"]
        ],
        "source_directory_removed": False,
        "error": None,
    }


def _persist_status(
    journal_dir: Path,
    journal: dict[str, Any],
    status: str,
    *,
    error: str | None = None,
) -> None:
    journal["status"] = status
    journal["updated_at"] = _now()
    journal["error"] = error
    _write_journal(journal_dir, journal)


def _assert_named_identity(
    root_fd: int,
    component: str,
    expected: Iterable[int],
    label: str,
) -> os.stat_result:
    current = _directory_child_stat(root_fd, component, label)
    if not _same_identity(current, expected):
        raise ChannelDesignationError(
            f"{label} changed since the dry run",
            409,
            code="stale_plan",
        )
    return current


def _relative_parts(relative: str) -> tuple[str, ...]:
    path = Path(relative)
    parts = path.parts
    if (
        not parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ChannelDesignationError(
            "Journal contains an unsafe relative path", 503,
            code="journal_unreadable",
        )
    for part in parts:
        _validated_component(part, "relative path component")
    return parts


def _open_relative_directory(base_fd: int, relative: str) -> int:
    descriptor = os.dup(base_fd)
    if not relative:
        return descriptor
    try:
        for part in _relative_parts(relative):
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _relative_parent_and_name(relative: str) -> tuple[str, str]:
    parts = _relative_parts(relative)
    return "/".join(parts[:-1]), parts[-1]


def _relative_entry_identity(base_fd: int, relative: str) -> tuple[int, int] | None:
    parent, name = _relative_parent_and_name(relative)
    parent_fd = _open_relative_directory(base_fd, parent)
    try:
        return _entry_identity_at(parent_fd, name)
    finally:
        os.close(parent_fd)


def _inventory_identity_view(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["kind"],
        item["device"],
        item["inode"],
        item.get("target") if item["kind"] == "symlink" else None,
    )


def _assert_inventory_matches(
    current: list[dict[str, Any]],
    expected: dict[str, dict[str, Any]],
    *,
    label: str,
) -> None:
    current_by_path = {item["path"]: item for item in current}
    if set(current_by_path) != set(expected):
        raise ChannelDesignationError(
            f"{label} changed during the designation operation",
            409,
            code="inventory_changed",
        )
    for relative_path, expected_item in expected.items():
        if _inventory_identity_view(current_by_path[relative_path]) != _inventory_identity_view(
            expected_item
        ):
            raise ChannelDesignationError(
                f"{label} entry changed during the designation operation: {relative_path}",
                409,
                code="inventory_changed",
            )


def _validate_applied_snapshot(
    journal: dict[str, Any],
    managed_roots: tuple[Path, ...],
) -> None:
    source = Path(journal["root"]) / journal["source_component"]
    destination = Path(journal["root"]) / journal["destination_component"]
    plan = journal["plan_snapshot"]
    current_destination, destination_blockers, _counts = _scan_tree(
        destination, destination
    )
    if destination_blockers:
        raise ChannelDesignationError(
            "Destination gained an unsafe entry during designation",
            409,
            code="inventory_changed",
        )
    if journal["mode"] == "rename":
        expected_destination = {item["path"]: item for item in plan["inventory"]}
        _assert_inventory_matches(
            current_destination,
            expected_destination,
            label="Renamed channel folder",
        )
    else:
        merged_paths = {
            item["relative_path"] for item in journal["merged_directories"]
        }
        expected_destination = {
            item["path"]: item for item in plan["destination_inventory"]
        }
        expected_destination.update({
            item["path"]: item
            for item in plan["inventory"]
            if item["path"] not in merged_paths
        })
        _assert_inventory_matches(
            current_destination,
            expected_destination,
            label="Consolidated destination folder",
        )
        current_source, source_blockers, _counts = _scan_tree(source, source)
        if source_blockers:
            raise ChannelDesignationError(
                "Old channel folder gained an unsafe entry during consolidation",
                409,
                code="inventory_changed",
            )
        source_by_path = {item["path"]: item for item in plan["inventory"]}
        expected_source = {
            relative_path: source_by_path[relative_path]
            for relative_path in merged_paths
        }
        _assert_inventory_matches(
            current_source,
            expected_source,
            label="Old consolidation skeleton",
        )
    inbound = _scan_inbound_links(source, managed_roots)
    if inbound:
        raise ChannelDesignationError(
            "A managed symlink began pointing into the old channel path during designation",
            409,
            code="inventory_changed",
        )


def _apply_filesystem(
    root_fd: int,
    journal_dir: Path,
    journal: dict[str, Any],
    managed_roots: tuple[Path, ...],
) -> None:
    source_component = journal["source_component"]
    destination_component = journal["destination_component"]
    _assert_named_identity(
        root_fd, source_component, journal["source_identity"], "Source channel folder"
    )
    if journal["mode"] == "rename":
        if _child_stat(root_fd, destination_component) is not None:
            raise ChannelDesignationError(
                "Destination appeared after the dry run", 409, code="stale_plan"
            )
        try:
            _rename_noreplace(root_fd, source_component, root_fd, destination_component)
        except FileExistsError as exc:
            raise ChannelDesignationError(
                "Destination appeared after the dry run", 409, code="stale_plan"
            ) from exc
        moved = _directory_child_stat(root_fd, destination_component, "Renamed folder")
        if not _same_identity(moved, journal["source_identity"]):
            raise ChannelDesignationError(
                "Renamed folder identity changed unexpectedly", 409,
                code="identity_mismatch",
            )
        _fsync_fd(root_fd)
        journal["source_directory_removed"] = True
        _validate_applied_snapshot(journal, managed_roots)
        _persist_status(journal_dir, journal, "filesystem_applied")
        return

    if journal["mode"] != "consolidate":
        raise ChannelDesignationError("Designation plan has no filesystem action", 409)

    _assert_named_identity(
        root_fd,
        destination_component,
        journal["destination_identity"],
        "Destination channel folder",
    )
    source_fd = os.open(source_component, _DIRECTORY_FLAGS, dir_fd=root_fd)
    destination_fd = os.open(destination_component, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        for operation in journal["operations"]:
            parent, name = _relative_parent_and_name(operation["relative_path"])
            # Keep both names pinned to the directories verified in the plan.
            _assert_named_identity(
                root_fd, source_component, journal["source_identity"], "Source channel folder"
            )
            _assert_named_identity(
                root_fd,
                destination_component,
                journal["destination_identity"],
                "Destination channel folder",
            )
            source_parent_fd = _open_relative_directory(source_fd, parent)
            destination_parent_fd = _open_relative_directory(destination_fd, parent)
            try:
                if not _same_identity(
                    os.fstat(source_parent_fd), operation["source_parent_identity"]
                ):
                    raise ChannelDesignationError(
                        f"Source parent changed since the dry run: {parent or '.'}",
                        409,
                        code="stale_plan",
                    )
                if not _same_identity(
                    os.fstat(destination_parent_fd),
                    operation["destination_parent_identity"],
                ):
                    raise ChannelDesignationError(
                        f"Destination parent changed since the dry run: {parent or '.'}",
                        409,
                        code="stale_plan",
                    )
                source_item = os.stat(
                    name, dir_fd=source_parent_fd, follow_symlinks=False
                )
                if not _same_identity(source_item, operation["identity"]):
                    raise ChannelDesignationError(
                        f"Source entry changed since the dry run: {operation['relative_path']}",
                        409,
                        code="stale_plan",
                    )
                if _child_stat(destination_parent_fd, name) is not None:
                    raise ChannelDesignationError(
                        f"Destination entry appeared after the dry run: {operation['relative_path']}",
                        409,
                        code="stale_plan",
                    )
                _rename_noreplace(
                    source_parent_fd, name, destination_parent_fd, name
                )
                moved = os.stat(
                    name, dir_fd=destination_parent_fd, follow_symlinks=False
                )
                if not _same_identity(moved, operation["identity"]):
                    raise ChannelDesignationError(
                        f"Moved entry identity changed unexpectedly: {operation['relative_path']}",
                        409,
                        code="identity_mismatch",
                    )
                _fsync_fd(source_parent_fd)
                if destination_parent_fd != source_parent_fd:
                    _fsync_fd(destination_parent_fd)
            finally:
                os.close(destination_parent_fd)
                os.close(source_parent_fd)
            operation["state"] = "moved"
            journal["updated_at"] = _now()
            _write_journal(journal_dir, journal)

        # Keep the original empty directory skeleton (and therefore its inode,
        # ownership, mode, ACLs, xattrs, and timestamps) until the DB CAS has
        # committed.  A failed DB update can then restore entries without
        # manufacturing a lookalike source folder.
        _validate_applied_snapshot(journal, managed_roots)
        _persist_status(journal_dir, journal, "filesystem_applied")
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _entry_identity_at(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return entry.st_dev, entry.st_ino


def _finish_consolidation_after_database(
    root_fd: int,
    journal_dir: Path,
    journal: dict[str, Any],
) -> None:
    """Remove only the verified, now-empty original directory skeleton."""

    if journal["mode"] != "consolidate":
        return
    source_component = journal["source_component"]
    source_state = _child_stat(root_fd, source_component)
    if source_state is None:
        journal["source_directory_removed"] = True
        return
    if not _same_identity(source_state, journal["source_identity"]):
        raise ChannelDesignationError(
            "A foreign directory appeared at the old channel path after database commit",
            409,
            code="recovery_blocked",
        )
    source_fd = os.open(source_component, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        for directory in sorted(
            journal["merged_directories"],
            key=lambda item: len(Path(item["relative_path"]).parts),
            reverse=True,
        ):
            if directory.get("state") == "removed":
                continue
            parent, name = _relative_parent_and_name(directory["relative_path"])
            try:
                source_parent_fd = _open_relative_directory(source_fd, parent)
            except FileNotFoundError:
                directory["state"] = "removed"
                continue
            try:
                current = _child_stat(source_parent_fd, name)
                if current is None:
                    directory["state"] = "removed"
                    continue
                if not stat.S_ISDIR(current.st_mode) or not _same_identity(
                    current, directory["source_identity"]
                ):
                    raise ChannelDesignationError(
                        f"Old directory changed after database commit: {directory['relative_path']}",
                        409,
                        code="recovery_blocked",
                    )
                os.rmdir(name, dir_fd=source_parent_fd)
                _fsync_fd(source_parent_fd)
            finally:
                os.close(source_parent_fd)
            directory["state"] = "removed"
            journal["updated_at"] = _now()
            _write_journal(journal_dir, journal)
    finally:
        os.close(source_fd)

    source_state = _child_stat(root_fd, source_component)
    if source_state is not None:
        if not _same_identity(source_state, journal["source_identity"]):
            raise ChannelDesignationError(
                "Old channel directory identity changed after database commit",
                409,
                code="recovery_blocked",
            )
        os.rmdir(source_component, dir_fd=root_fd)
        _fsync_fd(root_fd)
    journal["source_directory_removed"] = True
    journal["updated_at"] = _now()
    _write_journal(journal_dir, journal)


def _rollback_filesystem(
    root_fd: int,
    journal_dir: Path,
    journal: dict[str, Any],
) -> None:
    source_component = journal["source_component"]
    destination_component = journal["destination_component"]
    if journal["mode"] == "rename":
        source_state = _child_stat(root_fd, source_component)
        destination_state = _child_stat(root_fd, destination_component)
        if source_state is not None and _same_identity(source_state, journal["source_identity"]):
            # A foreign destination may have appeared before renameat2 ran.
            # The old tree is still intact, so there is nothing to undo and
            # the unrelated destination must be left untouched.
            pass
        elif (
            source_state is None
            and destination_state is not None
            and _same_identity(destination_state, journal["source_identity"])
        ):
            _rename_noreplace(root_fd, destination_component, root_fd, source_component)
            _fsync_fd(root_fd)
        else:
            raise ChannelDesignationError(
                "Channel folder identities no longer permit a safe rollback",
                409,
                code="recovery_blocked",
            )
        journal["source_directory_removed"] = False
        _persist_status(journal_dir, journal, "rolled_back")
        return

    destination_state = _assert_named_identity(
        root_fd,
        destination_component,
        journal["destination_identity"],
        "Destination channel folder",
    )
    del destination_state
    source_state = _child_stat(root_fd, source_component)
    if (
        source_state is None
        or not stat.S_ISDIR(source_state.st_mode)
        or stat.S_ISLNK(source_state.st_mode)
        or not _same_identity(source_state, journal["source_identity"])
    ):
        raise ChannelDesignationError(
            "The original channel directory is not intact; refusing rollback", 409,
            code="recovery_blocked",
        )
    source_fd = os.open(source_component, _DIRECTORY_FLAGS, dir_fd=root_fd)
    destination_fd = os.open(destination_component, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        for directory in journal["merged_directories"]:
            destination_identity = _relative_entry_identity(
                destination_fd, directory["relative_path"]
            )
            if destination_identity != tuple(directory["destination_identity"]):
                raise ChannelDesignationError(
                    f"Merged destination directory changed during rollback: {directory['relative_path']}",
                    409,
                    code="recovery_blocked",
                )
            source_identity = _relative_entry_identity(
                source_fd, directory["relative_path"]
            )
            if source_identity != tuple(directory["source_identity"]):
                raise ChannelDesignationError(
                    f"Original merged directory is not intact: {directory['relative_path']}",
                    409,
                    code="recovery_blocked",
                )
            directory["state"] = "present"

        for operation in reversed(journal["operations"]):
            parent, name = _relative_parent_and_name(operation["relative_path"])
            expected = tuple(operation["identity"])
            source_parent_fd = _open_relative_directory(source_fd, parent)
            destination_parent_fd = _open_relative_directory(destination_fd, parent)
            try:
                old_identity = _entry_identity_at(source_parent_fd, name)
                new_identity = _entry_identity_at(destination_parent_fd, name)
                if old_identity == expected and new_identity is None:
                    operation["state"] = "pending"
                    continue
                if old_identity is None and new_identity == expected:
                    _rename_noreplace(
                        destination_parent_fd, name, source_parent_fd, name
                    )
                    _fsync_fd(destination_parent_fd)
                    _fsync_fd(source_parent_fd)
                    operation["state"] = "pending"
                    journal["updated_at"] = _now()
                    _write_journal(journal_dir, journal)
                    continue
                raise ChannelDesignationError(
                    f"Entry identities no longer permit safe rollback: {operation['relative_path']}",
                    409,
                    code="recovery_blocked",
                )
            finally:
                os.close(destination_parent_fd)
                os.close(source_parent_fd)
        journal["source_directory_removed"] = False
        _persist_status(journal_dir, journal, "rolled_back")
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def apply_designation_plan(
    channel_id: str,
    source_component: str,
    plan_token: str,
    *,
    root: Path,
    revision: int | str,
    update_paths: PathUpdateCallback,
    designation: str = "terminated",
    destination_component: str | None = None,
    destination_channel_id: str | None = None,
    managed_roots: Iterable[Path] | None = None,
    journal_dir: Path | None = None,
    validate_precondition: PreconditionCallback | None = None,
    database_state: DatabaseStateCallback,
) -> dict[str, Any]:
    """Apply the exact current plan and update database paths atomically.

    ``update_paths`` is mandatory: a filesystem rename without the matching
    registry/path transaction would let later scans recreate the old folder.
    """

    if not isinstance(plan_token, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_token):
        raise ChannelDesignationError("plan_token must be the current dry-run token")
    if not callable(update_paths):
        raise ChannelDesignationError("A transactional database path updater is required", 503)
    if not callable(database_state):
        raise ChannelDesignationError(
            "An authoritative database-state callback is required", 503
        )
    roots = _normalize_managed_roots(managed_roots)
    journal_directory = _ensure_journal_dir(journal_dir)

    with designation_mutation_guard(journal_dir=journal_directory):
        plan = build_designation_plan(
            channel_id,
            source_component,
            root=root,
            designation=designation,
            revision=revision,
            destination_component=destination_component,
            destination_channel_id=destination_channel_id,
            managed_roots=roots,
        )
        if not secrets.compare_digest(plan_token, plan["plan_token"]):
            raise ChannelDesignationError(
                "Designation plan changed; review the dry run again",
                409,
                code="stale_plan",
            )
        if plan["blockers"]:
            raise ChannelDesignationError(
                "Designation plan has safety blockers",
                409,
                code="plan_blocked",
            )
        if plan["mode"] == "noop":
            raise ChannelDesignationError("Channel folder is already designated", 409)
        if validate_precondition is not None:
            validate_precondition(plan)

        journal = _journal_from_plan(plan)
        _write_journal(journal_directory, journal)
        source = Path(plan["source_path"])
        destination = Path(plan["destination_path"])
        database_committed = False
        database_outcome_uncertain = False
        try:
            with media_mutation_guard(
                trees=(source, destination),
                action="designate this channel while its media is being processed",
            ):
                with _open_root(Path(root), roots) as (_canonical, root_fd, root_stat):
                    if not _same_identity(root_stat, plan["root_identity"]):
                        raise ChannelDesignationError(
                            "Managed root changed since the dry run", 409,
                            code="stale_plan",
                        )
                    _apply_filesystem(root_fd, journal_directory, journal, roots)
                    mutation = {
                        **journal,
                        "old_path": str(source),
                        "new_path": str(destination),
                    }
                    try:
                        update_paths(mutation)
                        database_committed = True
                    except Exception as exc:
                        try:
                            state = database_state(journal)
                        except Exception as state_exc:
                            database_outcome_uncertain = True
                            _persist_status(
                                journal_directory,
                                journal,
                                "recovery_blocked",
                                error=f"Database update failed and state check failed: {exc}; {state_exc}",
                            )
                            raise ChannelDesignationError(
                                "Database outcome is uncertain; journal recovery is required",
                                503,
                                code="recovery_blocked",
                            ) from state_exc
                        if state == "new":
                            database_committed = True
                        elif state not in {"old"}:
                            database_outcome_uncertain = True
                            _persist_status(
                                journal_directory,
                                journal,
                                "recovery_blocked",
                                error=f"Database update outcome is {state}: {exc}",
                            )
                            raise ChannelDesignationError(
                                "Database outcome is uncertain; journal recovery is required",
                                503,
                                code="recovery_blocked",
                            ) from exc
                        else:
                            raise
                    if database_committed:
                        _persist_status(journal_directory, journal, "database_applied")
                        _finish_consolidation_after_database(
                            root_fd, journal_directory, journal
                        )
                        _persist_status(journal_directory, journal, "committed")
        except Exception as exc:
            if database_committed:
                # The filesystem and DB agree.  A journal-finalization failure
                # must be recovered by inspecting DB state, never by undoing
                # the already committed database transaction.
                raise ChannelDesignationError(
                    "Designation committed but its journal needs finalization",
                    503,
                    code="journal_finalize_required",
                ) from exc
            if database_outcome_uncertain:
                if isinstance(exc, ChannelDesignationError):
                    raise
                raise ChannelDesignationError(
                    "Database outcome is uncertain; journal recovery is required",
                    503,
                    code="recovery_blocked",
                ) from exc
            try:
                with _open_root(Path(root), roots) as (_canonical, root_fd, _root_stat):
                    _persist_status(
                        journal_directory, journal, "rolling_back", error=str(exc)
                    )
                    _rollback_filesystem(root_fd, journal_directory, journal)
            except Exception as rollback_exc:
                try:
                    _persist_status(
                        journal_directory,
                        journal,
                        "recovery_blocked",
                        error=f"{exc}; rollback failed: {rollback_exc}",
                    )
                except Exception:
                    pass
                raise ChannelDesignationError(
                    "Designation failed and automatic rollback could not be proven safe",
                    503,
                    code="recovery_blocked",
                ) from rollback_exc
            if isinstance(exc, ChannelDesignationError):
                raise
            if isinstance(exc, MediaReservationConflict):
                raise ChannelDesignationError(str(exc), 409, code="media_busy") from exc
            raise ChannelDesignationError(
                f"Designation failed and was rolled back: {exc}",
                409,
                code="apply_failed",
            ) from exc

        return {
            "success": True,
            "journal_id": journal["journal_id"],
            "status": journal["status"],
            "channel_id": channel_id,
            "designation": designation,
            "old_path": str(source),
            "new_path": str(destination),
            "mode": plan["mode"],
            "counts": plan["counts"],
        }


def list_designation_journals(
    *,
    journal_dir: Path | None = None,
    include_terminal: bool = False,
) -> list[dict[str, Any]]:
    directory = _ensure_journal_dir(journal_dir)
    journals: list[dict[str, Any]] = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise ChannelDesignationError(f"Could not list designation journals: {exc}", 503) from exc
    for path in paths:
        journal = _load_journal(path)
        if include_terminal or journal.get("status") not in _TERMINAL_JOURNAL_STATES:
            journals.append(journal)
    return journals


@contextmanager
def designation_mutation_guard(
    *, journal_dir: Path | None = None
) -> Iterator[Path]:
    """Serialize a designation and reject unresolved prior transactions."""

    directory = _ensure_journal_dir(journal_dir)
    with _cross_process_lock(directory), LIBRARY_MUTATION_LOCK:
        unresolved = list_designation_journals(journal_dir=directory)
        if unresolved:
            identifiers = ", ".join(
                str(item.get("journal_id") or "unknown") for item in unresolved[:3]
            )
            raise ChannelDesignationError(
                f"Resolve existing channel-designation journal(s) before applying a new plan: {identifiers}",
                409,
                code="unresolved_journal",
            )
        yield directory


def _filesystem_applied(root_fd: int, journal: dict[str, Any]) -> bool:
    source_state = _child_stat(root_fd, journal["source_component"])
    destination_state = _child_stat(root_fd, journal["destination_component"])
    if journal["mode"] == "rename":
        return (
            source_state is None
            and destination_state is not None
            and _same_identity(destination_state, journal["source_identity"])
        )
    if (
        destination_state is None
        or not _same_identity(destination_state, journal["destination_identity"])
    ):
        return False
    if source_state is not None and not _same_identity(
        source_state, journal["source_identity"]
    ):
        return False
    destination_fd = os.open(journal["destination_component"], _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        return all(
            _relative_entry_identity(destination_fd, operation["relative_path"])
            == tuple(operation["identity"])
            for operation in journal["operations"]
        )
    finally:
        os.close(destination_fd)


def recover_designation_journals(
    database_state: DatabaseStateCallback,
    *,
    managed_roots: Iterable[Path] | None = None,
    journal_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Resolve incomplete journals using authoritative database path state.

    ``new`` finalizes an already committed mutation. ``old`` rolls back any
    observed filesystem moves. ``mixed``/``unknown`` are left blocked for
    operator intervention; guessing could make valid real files unreachable.
    """

    if not callable(database_state):
        raise ChannelDesignationError("A database-state callback is required", 503)
    directory = _ensure_journal_dir(journal_dir)
    results: list[dict[str, Any]] = []
    with _cross_process_lock(directory), LIBRARY_MUTATION_LOCK:
        journals = list_designation_journals(journal_dir=directory)
        if not journals:
            return results
        # Fresh installations may not have created every managed media root
        # yet. They need no recovery. Once a nonterminal journal exists,
        # however, every recorded root must be available or startup fails
        # closed without guessing at filesystem state.
        roots = _normalize_managed_roots(managed_roots)
        for journal in journals:
            state = database_state(journal)
            root = Path(journal["root"])
            if state not in {"old", "new", "mixed", "unknown"}:
                state = "unknown"
            try:
                with media_mutation_guard(
                    trees=(
                        root / journal["source_component"],
                        root / journal["destination_component"],
                    ),
                    action="recover this channel-folder designation",
                ):
                    with _open_root(root, roots) as (_canonical, root_fd, root_stat):
                        if not _same_identity(root_stat, journal["root_identity"]):
                            raise ChannelDesignationError(
                                "Managed root identity changed since the journal was written",
                                409,
                                code="recovery_blocked",
                            )
                        if state == "new":
                            if not _filesystem_applied(root_fd, journal):
                                raise ChannelDesignationError(
                                    "Database uses new paths but the filesystem does not match",
                                    409,
                                    code="recovery_blocked",
                                )
                            _persist_status(directory, journal, "database_applied")
                            _finish_consolidation_after_database(
                                root_fd, directory, journal
                            )
                            _persist_status(directory, journal, "committed")
                        elif state == "old":
                            _rollback_filesystem(root_fd, directory, journal)
                        else:
                            raise ChannelDesignationError(
                                f"Database path state is {state}",
                                409,
                                code="recovery_blocked",
                            )
            except Exception as exc:
                try:
                    _persist_status(
                        directory, journal, "recovery_blocked", error=str(exc)
                    )
                except Exception:
                    pass
            results.append(journal)
    return results
