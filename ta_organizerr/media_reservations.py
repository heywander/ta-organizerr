"""Short-lived coordination around long-running media readers.

The application already serializes filesystem writers with
``LIBRARY_MUTATION_LOCK``.  A salvage or transcode job cannot hold that lock
for its entire FFmpeg run, though, so it registers the inode and user-visible
path entries it depends on here.  Writers briefly take the library lock,
check this registry, perform their mutation, and release the lock again.

This registry is deliberately process-local, matching the existing library
mutation lock.  It protects application workers in this process; filesystem
permissions and descriptor/path revalidation remain the boundary against
uncoordinated external writers.
"""
from __future__ import annotations

import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .library_lock import LIBRARY_MUTATION_LOCK


MediaIdentity = tuple[int, int]


@dataclass(frozen=True)
class MediaReservation:
    """Opaque lease returned to one long-running media operation."""

    token: str
    owner: str
    identities: frozenset[MediaIdentity]
    paths: frozenset[Path]


class MediaReservationConflict(OSError):
    """Raised when an operation would invalidate an active media lease."""

    def __init__(self, action: str, reservation: MediaReservation):
        self.action = action
        self.reservation = reservation
        super().__init__(
            f"Cannot {action}; media is currently in use by {reservation.owner}"
        )


_registry_lock = threading.RLock()
_by_token: dict[str, MediaReservation] = {}
_by_identity: dict[MediaIdentity, str] = {}
_by_path: dict[Path, str] = {}


def media_identity(file_stat: os.stat_result) -> MediaIdentity:
    """Return the stable device/inode identity used by the registry."""

    return file_stat.st_dev, file_stat.st_ino


def _entry_path(path: str | Path) -> Path:
    """Normalize a directory entry without following its final symlink."""

    candidate = Path(path)
    try:
        return candidate.parent.resolve(strict=False) / candidate.name
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(candidate))


def _conflict_for(
    identities: Iterable[MediaIdentity],
    paths: Iterable[Path],
    *,
    except_token: str | None,
) -> MediaReservation | None:
    for identity in identities:
        token = _by_identity.get(identity)
        if token is not None and token != except_token:
            return _by_token[token]
    for path in paths:
        token = _by_path.get(path)
        if token is not None and token != except_token:
            return _by_token[token]
    return None


def reserve_media(
    identities: Iterable[MediaIdentity],
    *,
    paths: Iterable[str | Path] = (),
    owner: str,
) -> MediaReservation:
    """Atomically reserve all supplied inodes and path entries.

    The library lock is held only while the in-memory lease is established.
    Callers that opened a descriptor before this call should revalidate it and
    its requested path while holding ``LIBRARY_MUTATION_LOCK`` immediately
    before reserving.
    """

    normalized_identities = frozenset(identities)
    if not normalized_identities:
        raise ValueError("At least one media identity is required")
    if not owner:
        raise ValueError("Reservation owner is required")

    with LIBRARY_MUTATION_LOCK:
        normalized_paths = frozenset(_entry_path(path) for path in paths)
        with _registry_lock:
            conflict = _conflict_for(
                normalized_identities,
                normalized_paths,
                except_token=None,
            )
            if conflict is not None:
                raise MediaReservationConflict("reserve this media", conflict)
            token = secrets.token_hex(16)
            reservation = MediaReservation(
                token=token,
                owner=owner,
                identities=normalized_identities,
                paths=normalized_paths,
            )
            _by_token[token] = reservation
            for identity in normalized_identities:
                _by_identity[identity] = token
            for path in normalized_paths:
                _by_path[path] = token
            return reservation


def release_media(reservation: MediaReservation | str | None) -> None:
    """Release a lease; repeated or late cleanup is harmless."""

    if reservation is None:
        return
    token = (
        reservation.token
        if isinstance(reservation, MediaReservation)
        else reservation
    )
    with LIBRARY_MUTATION_LOCK:
        with _registry_lock:
            stored = _by_token.pop(token, None)
            if stored is None:
                return
            for identity in stored.identities:
                if _by_identity.get(identity) == token:
                    _by_identity.pop(identity, None)
            for path in stored.paths:
                if _by_path.get(path) == token:
                    _by_path.pop(path, None)


def extend_media_reservation(
    reservation: MediaReservation | str,
    identities: Iterable[MediaIdentity],
    *,
    paths: Iterable[str | Path] = (),
) -> MediaReservation:
    """Atomically add newly created output inodes/paths to an active lease."""

    token = (
        reservation.token
        if isinstance(reservation, MediaReservation)
        else reservation
    )
    added_identities = frozenset(identities)
    with LIBRARY_MUTATION_LOCK:
        added_paths = frozenset(_entry_path(path) for path in paths)
        if not added_identities and not added_paths:
            raise ValueError("At least one media identity or path is required")
        with _registry_lock:
            stored = _by_token.get(token)
            if stored is None:
                raise ValueError("Media reservation is no longer active")
            conflict = _conflict_for(
                added_identities,
                added_paths,
                except_token=token,
            )
            if conflict is not None:
                raise MediaReservationConflict("reserve this generated media", conflict)
            updated = MediaReservation(
                token=stored.token,
                owner=stored.owner,
                identities=stored.identities | added_identities,
                paths=stored.paths | added_paths,
            )
            _by_token[token] = updated
            for identity in added_identities:
                _by_identity[identity] = token
            for path in added_paths:
                _by_path[path] = token
            return updated


def _path_identities(paths: Iterable[Path]) -> set[MediaIdentity]:
    identities: set[MediaIdentity] = set()
    for path in paths:
        try:
            entry_stat = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(entry_stat.st_mode):
            identities.add(media_identity(entry_stat))
    return identities


def _tree_conflict(
    roots: Iterable[Path],
    *,
    except_token: str | None,
) -> MediaReservation | None:
    for reserved_path, token in _by_path.items():
        if token == except_token:
            continue
        for root in roots:
            try:
                if reserved_path == root or reserved_path.is_relative_to(root):
                    return _by_token[token]
            except (OSError, RuntimeError, ValueError):
                continue
    return None


@contextmanager
def media_mutation_guard(
    *,
    paths: Iterable[str | Path] = (),
    identities: Iterable[MediaIdentity] = (),
    trees: Iterable[str | Path] = (),
    except_reservation: MediaReservation | str | None = None,
    action: str = "modify this media",
) -> Iterator[None]:
    """Hold the writer lock and reject mutations that invalidate a lease.

    ``paths`` protects both an exact requested entry (including a symlink) and
    any regular inode currently at that entry.  ``trees`` protects path moves
    or removals of any directory containing a reserved entry.  The registry
    lock is released before yielding, while ``LIBRARY_MUTATION_LOCK`` remains
    held so no application worker can establish a new lease mid-mutation.
    """

    token = (
        except_reservation.token
        if isinstance(except_reservation, MediaReservation)
        else except_reservation
    )
    requested_identities = set(identities)

    with LIBRARY_MUTATION_LOCK:
        normalized_paths = tuple(_entry_path(path) for path in paths)
        normalized_trees = tuple(_entry_path(path) for path in trees)
        requested_identities.update(_path_identities(normalized_paths))
        with _registry_lock:
            conflict = _conflict_for(
                requested_identities,
                normalized_paths,
                except_token=token,
            )
            if conflict is None:
                conflict = _tree_conflict(normalized_trees, except_token=token)
            if conflict is not None:
                raise MediaReservationConflict(action, conflict)
        yield


def _reset_reservations_for_tests() -> None:
    """Clear process-local state after tests have stopped all workers."""

    with LIBRARY_MUTATION_LOCK:
        with _registry_lock:
            _by_token.clear()
            _by_identity.clear()
            _by_path.clear()
